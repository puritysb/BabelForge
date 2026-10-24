"""Process-safe queue of optional device deliveries."""
from __future__ import annotations
import os
import time
from typing import Optional
import config
from state_store import json_transaction, read_json


def _valid(items):
    return (isinstance(items, list) and all(
        isinstance(item, dict) and isinstance(item.get("epub_path"), str)
        for item in items))


def _transaction():
    return json_transaction(config.PENDING_PUSH_PATH, list, _valid)


def enqueue(epub_path: str, title: str, author: str,
            source: str = "", rights: str = "", req_id: Optional[str] = None,
            notify: bool = False) -> None:
    if not os.path.isfile(epub_path):
        raise FileNotFoundError(epub_path)
    with _transaction() as items:
        items[:] = [item for item in items if item["epub_path"] != epub_path]
        items.append({"epub_path": epub_path, "title": title, "author": author,
                      "source": source, "rights": rights, "req_id": req_id,
                      "notify": notify,
                      "queued_at_ms": int(time.time() * 1000), "attempts": 0})


def remove(epub_path: str) -> None:
    with _transaction() as items:
        items[:] = [item for item in items if item["epub_path"] != epub_path]


def list_pending() -> list[dict]:
    return read_json(config.PENDING_PUSH_PATH, list, _valid)


def increment_attempts(epub_path: str) -> None:
    with _transaction() as items:
        for item in items:
            if item["epub_path"] == epub_path:
                item["attempts"] = item.get("attempts", 0) + 1
                item["last_attempt_ms"] = int(time.time() * 1000)
