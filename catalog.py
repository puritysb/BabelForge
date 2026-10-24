"""Request catalog with process-safe transactions and worker ownership."""
from __future__ import annotations
from contextlib import contextmanager
import os
import re
import sys
import time
import uuid
from typing import Optional
import config
from state_store import file_lock, json_transaction, read_json

WORKER_START_GRACE_MS = 60_000
TERMINAL_STATES = {"failed", "published", "completed"}


class RequestAlreadyRunning(RuntimeError):
    pass


def _valid(data):
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return False
    ids = []
    for entry in data["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            return False
        ids.append(entry["id"])
    return len(ids) == len(set(ids))


def _transaction():
    return json_transaction(config.CATALOG_PATH, lambda: {"entries": []}, _valid)


def _load():
    return read_json(config.CATALOG_PATH, lambda: {"entries": []}, _valid)


def _entry(data, req_id):
    for entry in data["entries"]:
        if entry["id"] == req_id:
            return entry
    raise KeyError(f"request id not found: {req_id}")


def _touch(entry, **fields):
    entry.update(fields)
    entry["updated_ms"] = int(time.time() * 1000)
    return dict(entry)


def new_request(query: str) -> str:
    req_id = uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)
    with _transaction() as data:
        data["entries"].append({
            "id": req_id, "query": query, "status": "requested",
            "created_ms": now, "updated_ms": now, "candidates": [],
            "selected": None, "epub_path": None, "opds_url": None,
            "progress": None, "error": None,
        })
    return req_id


def update(req_id: str, **fields) -> dict:
    with _transaction() as data:
        result = _touch(_entry(data, req_id), **fields)
    return result


def set_candidates(req_id: str, candidates: list[dict]) -> None:
    update(req_id, status="searching", candidates=candidates)


def select(req_id: str, candidate: dict) -> None:
    update(req_id, status="selected", selected=candidate)


def set_progress(req_id: str, done: int, total: int) -> None:
    update(req_id, progress={"done": done, "total": total,
                            "pct": round(100 * done / total, 1) if total else 0})


def mark_failed(req_id: str, error: str) -> None:
    update(req_id, status="failed", error=error)


def mark_published(req_id: str, epub_path: str, opds_url: str) -> None:
    update(req_id, status="published", epub_path=epub_path, opds_url=opds_url,
           progress={"done": 100, "total": 100, "pct": 100.0}, error=None)


def mark_completed(req_id: str, epub_path: str) -> None:
    update(req_id, status="completed", epub_path=epub_path, opds_url=None,
           progress={"done": 100, "total": 100, "pct": 100.0}, error=None)


def get(req_id: str) -> Optional[dict]:
    try:
        return _entry(_load(), req_id)
    except KeyError:
        return None


def recent(limit: int = 20) -> list[dict]:
    return sorted(_load()["entries"],
                  key=lambda e: e.get("updated_ms") or e.get("created_ms") or 0,
                  reverse=True)[:max(0, limit)]


def _worker_path(req_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", req_id):
        raise ValueError("invalid request id")
    return os.path.join(config.CHECKPOINT_DIR, req_id + ".worker.lock")


def _worker_locked(req_id):
    try:
        with file_lock(_worker_path(req_id), blocking=False):
            return False
    except BlockingIOError:
        return True


def _pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    # A detached child can exit before importing pipeline.py. Reap it when
    # this is its parent instead of treating a zombie as a living worker.
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _busy(entry):
    if _worker_locked(entry["id"]):
        return True
    # Once a worker has acquired the lock, a free lock is authoritative even
    # if its old PID has since been reused by an unrelated process.
    if entry.get("worker_started_ms") and entry.get("worker_pid"):
        return False
    if entry.get("worker_pid"):
        return _pid_alive(entry["worker_pid"])
    claimed = entry.get("worker_claimed_ms")
    return bool(claimed and int(time.time() * 1000) - claimed < WORKER_START_GRACE_MS)


@contextmanager
def request_worker(req_id: str):
    """Hold for the entire pipeline run; process exit releases the lock."""
    try:
        lock = file_lock(_worker_path(req_id), blocking=False)
        lock.__enter__()
    except BlockingIOError as exc:
        raise RequestAlreadyRunning(f"request {req_id} already has a worker") from exc
    try:
        update(req_id, worker_pid=os.getpid(),
               worker_started_ms=int(time.time() * 1000), worker_claimed_ms=None)
        yield
    finally:
        try:
            with _transaction() as data:
                entry = _entry(data, req_id)
                if entry.get("worker_pid") == os.getpid():
                    if entry.get("status") not in TERMINAL_STATES:
                        _touch(entry, status="failed",
                               error="worker exited before completion; retry_request can resume")
                    _touch(entry, last_worker_pid=os.getpid(), worker_pid=None,
                           worker_claimed_ms=None, worker_finished_ms=int(time.time() * 1000))
        except Exception as exc:
            # A cleanup write is auxiliary: preserve a completed artifact and
            # the original exception, if any. The free OS lock still proves
            # this worker is no longer running when status is reconciled.
            sys.stderr.write(f"[catalog] worker cleanup could not be recorded: {exc}\n")
        finally:
            lock.__exit__(None, None, None)


def prepare_worker(req_id: str, candidate: dict, publish_output: bool = True,
                   notify: bool = False):
    with _transaction() as data:
        entry = _entry(data, req_id)
        if _busy(entry):
            raise RequestAlreadyRunning(f"request {req_id} already has a worker")
        _touch(entry, status="launching", selected=candidate, error=None,
               worker_pid=None, worker_started_ms=None,
               publish_output=publish_output, notify=notify,
               worker_claimed_ms=int(time.time() * 1000))


def record_worker_spawn(req_id: str, pid: int):
    with _transaction() as data:
        entry = _entry(data, req_id)
        if (entry.get("status") == "launching" and entry.get("worker_pid") is None
                and not entry.get("worker_started_ms")):
            _touch(entry, worker_pid=pid)


def claim_retry(req_id: str, publish_output: Optional[bool] = None,
                notify: Optional[bool] = None) -> dict:
    """Atomically claim a failed or dead request before spawning its worker."""
    with _transaction() as data:
        entry = _entry(data, req_id)
        if entry.get("status") in {"published", "completed"}:
            raise ValueError("completed requests cannot be retried")
        if _busy(entry):
            raise RequestAlreadyRunning("request worker is still running or starting")
        if not isinstance(entry.get("selected"), dict):
            raise ValueError("request has no persisted candidate")
        candidate = dict(entry["selected"])
        _touch(entry, status="launching", error=None, progress=None,
               concept_sheet_path=None, concepts=None, precision_profile=None,
               quality_report=None, worker_pid=None, worker_started_ms=None,
               publish_output=(entry.get("publish_output", True)
                               if publish_output is None else publish_output),
               notify=(entry.get("notify") is True if notify is None else notify),
               worker_claimed_ms=int(time.time() * 1000),
               retry_count=int(entry.get("retry_count") or 0) + 1)
    return candidate


def reconcile_worker(req_id: str) -> Optional[dict]:
    """Surface a crashed detached worker as a recoverable failure."""
    with _transaction() as data:
        entry = _entry(data, req_id)
        has_worker = entry.get("worker_pid") or entry.get("worker_claimed_ms")
        if entry.get("status") not in TERMINAL_STATES and has_worker and not _busy(entry):
            _touch(entry, status="failed", error="worker exited before completion; retry_request can resume")
        result = dict(entry)
    return result
