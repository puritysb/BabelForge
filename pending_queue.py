"""Pending push queue — books that failed/skipped device push and should be
retried automatically when the device next appears.

Stored as data/pending-push.json. publish() success + push skip/failure lands
here. The auto_push_watcher drains the queue whenever it discovers the device.

Why a separate file (not catalog.json): the watcher runs as a different
process under launchd with a different cadence. A small, append-only queue
file keeps concurrency simple and survives restarts.
"""
from __future__ import annotations
import json
import os
import time
from typing import Optional

import config


def _load() -> list[dict]:
    if not os.path.isfile(config.PENDING_PUSH_PATH):
        return []
    try:
        with open(config.PENDING_PUSH_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _atomic_write(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(config.PENDING_PUSH_PATH), exist_ok=True)
    tmp = config.PENDING_PUSH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config.PENDING_PUSH_PATH)


def enqueue(epub_path: str, title: str, author: str,
            source: str = "", rights: str = "",
            req_id: Optional[str] = None) -> None:
    """Add a book to the pending push queue. Idempotent — re-enqueueing the
    same path just refreshes its timestamp (no duplicates)."""
    if not os.path.isfile(epub_path):
        return
    items = _load()
    # De-dup by epub_path: if it's already queued, update timestamp + metadata.
    items = [it for it in items if it.get("epub_path") != epub_path]
    items.append({
        "epub_path": epub_path,
        "title": title,
        "author": author,
        "source": source,
        "rights": rights,
        "req_id": req_id,
        "queued_at_ms": int(time.time() * 1000),
        "attempts": 0,
    })
    _atomic_write(items)


def remove(epub_path: str) -> None:
    items = [it for it in _load() if it.get("epub_path") != epub_path]
    _atomic_write(items)


def list_pending() -> list[dict]:
    return _load()


def increment_attempts(epub_path: str) -> None:
    items = _load()
    for it in items:
        if it.get("epub_path") == epub_path:
            it["attempts"] = it.get("attempts", 0) + 1
            it["last_attempt_ms"] = int(time.time() * 1000)
    _atomic_write(items)
