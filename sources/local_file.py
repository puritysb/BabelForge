"""Local file ingest adapter.

The user already owns the source file (EPUB or .txt). This adapter never
searches — candidates are constructed directly from a filesystem path. Caller
passes the path as `query`; we return a single-element candidate list.

Copyright is the user's responsibility.
"""
import os
from sources import Candidate, SourceText
import config


def search(query: str, limit: int = 1) -> list[Candidate]:
    """`query` is a filesystem path to an EPUB or .txt owned by the user."""
    path = os.path.expanduser(query.strip())
    if not os.path.isfile(path):
        raise FileNotFoundError(f"local ingest: not a file: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".epub", ".txt"):
        raise ValueError(f"local ingest: unsupported extension {ext!r} (want .epub/.txt)")
    title = os.path.splitext(os.path.basename(path))[0]
    return [Candidate(
        source="local",
        source_id=os.path.abspath(path),
        title=title,
        author="Unknown (local file)",
        language="en",
        rights="user-supplied (user responsibility)",
        fetch_url=f"file://{os.path.abspath(path)}",
        extra={"local_path": os.path.abspath(path)},
    )]


def fetch(candidate: Candidate) -> SourceText:
    p = candidate.extra.get("local_path") if candidate.extra else None
    if not p and candidate.fetch_url and candidate.fetch_url.startswith("file://"):
        p = candidate.fetch_url[7:]
    if not p or not os.path.isfile(p):
        raise FileNotFoundError(f"local file missing: {p}")
    ext = "txt" if p.lower().endswith(".txt") else "epub"
    return SourceText(candidate=candidate, format=ext, path=p)
