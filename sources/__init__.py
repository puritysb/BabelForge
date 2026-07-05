"""Pluggable source adapters for the book_translator pipeline.

Each adapter exposes two functions:
  - search(query, limit=N) -> list[Candidate]
  - fetch(candidate) -> SourceText

A Candidate carries everything needed to (a) show the user a pick list and
(b) download the book later. SourceText is the raw extracted payload (path to
an .epub/.txt or an in-memory string) handed to extract.py.

Drop-in new sources by adding a module here and registering it in ADAPTERS.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Candidate:
    source: str            # adapter key: "gutenberg", "standard_ebooks", ...
    source_id: str         # id within that source (e.g. gutenberg book id)
    title: str
    author: str
    language: str = "en"
    year: Optional[int] = None
    rights: Optional[str] = None    # "public domain", "copyright", "unknown"
    fetch_url: Optional[str] = None  # direct download URL, if known at search time
    extra: Optional[dict] = None     # adapter-specific payload

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SourceText:
    candidate: Candidate
    format: str            # "epub" or "txt"
    path: str              # local file path after fetch()
    chapters_hint: Optional[int] = None


# Lazy imports inside register() to keep `import sources` cheap.
ADAPTERS = {
    "gutenberg": "sources.gutenberg",
    "standard_ebooks": "sources.standard_ebooks",
    "annas_archive": "sources.annas_archive",
    "local": "sources.local_file",
}


def get_adapter(name: str):
    if name not in ADAPTERS:
        raise KeyError(f"unknown source: {name!r} (known: {sorted(ADAPTERS)})")
    mod = __import__(ADAPTERS[name], fromlist=["search", "fetch"])
    return mod
