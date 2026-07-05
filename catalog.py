"""Atomic catalog state machine (yt_dubber pattern).

States: requested → searching → selected → translating → assembling → published
                                                              ↘ failed

The catalog is a single JSON file with a list of entries keyed by request id.
Writes go to a .tmp file then os.replace() — concurrent-safe on POSIX.
"""
from __future__ import annotations
import json
import os
import time
import uuid
from typing import Optional

import config


def _load() -> dict:
    if not os.path.exists(config.CATALOG_PATH):
        return {"entries": []}
    try:
        with open(config.CATALOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"entries": []}


def _atomic_write(data: dict) -> None:
    os.makedirs(os.path.dirname(config.CATALOG_PATH), exist_ok=True)
    tmp = config.CATALOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config.CATALOG_PATH)


def new_request(query: str) -> str:
    req_id = uuid.uuid4().hex[:12]
    data = _load()
    entry = {
        "id": req_id,
        "query": query,
        "status": "requested",
        "created_ms": int(time.time() * 1000),
        "updated_ms": int(time.time() * 1000),
        "candidates": [],
        "selected": None,
        "epub_path": None,
        "opds_url": None,
        "progress": None,
        "error": None,
    }
    data["entries"].append(entry)
    _atomic_write(data)
    return req_id


def update(req_id: str, **fields) -> dict:
    data = _load()
    for e in data["entries"]:
        if e["id"] == req_id:
            e.update(fields)
            e["updated_ms"] = int(time.time() * 1000)
            _atomic_write(data)
            return e
    raise KeyError(f"request id not found: {req_id}")


def set_candidates(req_id: str, candidates: list[dict]) -> None:
    update(req_id, status="searching", candidates=candidates)


def select(req_id: str, candidate: dict) -> None:
    update(req_id, status="selected", selected=candidate)


def set_progress(req_id: str, done: int, total: int) -> None:
    pct = round(100 * done / total, 1) if total else 0
    update(req_id, progress={"done": done, "total": total, "pct": pct})


def mark_failed(req_id: str, error: str) -> None:
    update(req_id, status="failed", error=error)


def mark_published(req_id: str, epub_path: str, opds_url: str) -> None:
    # Clear a stale `error` from an earlier failed attempt on this same
    # req_id (e.g. a resumed request) — otherwise a published record keeps
    # showing the old failure message forever, which is what get_status()
    # (CLI and the babelforge MCP tool) hands straight to callers.
    update(req_id, status="published", epub_path=epub_path, opds_url=opds_url,
           progress={"done": 100, "total": 100, "pct": 100.0}, error=None)


def get(req_id: str) -> Optional[dict]:
    data = _load()
    for e in data["entries"]:
        if e["id"] == req_id:
            return e
    return None


def recent(limit: int = 20) -> list[dict]:
    data = _load()
    return data.get("entries", [])[-limit:][::-1]
