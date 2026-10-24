"""Locked JSON transactions shared by the catalog and push queue."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json
import os
import tempfile


class StateError(RuntimeError):
    """Unreadable state must never silently become empty state."""


@contextmanager
def file_lock(path: str, *, blocking: bool = True):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read(path, factory, validate):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return factory()
    except (OSError, ValueError) as exc:
        raise StateError(f"Cannot read state file {path}: {exc}") from exc
    if not validate(data):
        raise StateError(f"Invalid state schema in {path}")
    return data


def _write(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, factory, validate):
    with file_lock(path + ".lock"):
        return _read(path, factory, validate)


@contextmanager
def json_transaction(path, factory, validate):
    with file_lock(path + ".lock"):
        data = _read(path, factory, validate)
        yield data
        if not validate(data):
            raise StateError(f"Refusing invalid state for {path}")
        _write(path, data)
