"""Project Gutenberg adapter via the gutendex API.

Public domain. The safest default source for auto-suggest.
"""
import os
import requests
from sources import Candidate, SourceText
import config

API = config.GUTENDEX_API
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "OpenClaw-book_translator/0.1 (+local)"


def search(query: str, limit: int = config.MAX_CANDIDATES) -> list[Candidate]:
    """Search gutendex. query is free text (title/author)."""
    out: list[Candidate] = []
    url = API
    params = {"search": query}
    while url and len(out) < limit:
        r = SESSION.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        for b in data.get("results", []):
            authors = ", ".join(a.get("name", "") for a in b.get("authors", []))
            langs = b.get("languages") or ["en"]
            rights = "public domain" if b.get("copyright") is False else (
                "copyright" if b.get("copyright") else "unknown"
            )
            # prefer plain text utf-8; fall back to html
            fmt = b.get("formats", {})
            fetch_url = fmt.get("text/plain; charset=utf-8") \
                or fmt.get("text/plain; charset=us-ascii") \
                or fmt.get("text/html; charset=utf-8") \
                or fmt.get("application/epub+zip")
            out.append(Candidate(
                source="gutenberg",
                source_id=str(b["id"]),
                title=b.get("title", "").strip(),
                author=authors,
                language=langs[0] if langs else "en",
                rights=rights,
                fetch_url=fetch_url,
                extra={"gutenberg_id": b["id"]},
            ))
            if len(out) >= limit:
                break
        url = data.get("next")
        params = None  # next already carries query
    return out


def fetch(candidate: Candidate) -> SourceText:
    """Download the book text to cache; return path."""
    if not candidate.fetch_url:
        raise ValueError(f"no fetch_url for {candidate}")
    ext = ".epub" if candidate.fetch_url.endswith(".epub") or "epub" in candidate.fetch_url else ".txt"
    cache_dir = os.path.join(config.CACHE_DIR,
                             f"{candidate.source}-{candidate.source_id}")
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, f"book{ext}")
    if not os.path.exists(dest):
        r = SESSION.get(candidate.fetch_url, timeout=120)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
    fmt = "epub" if ext == ".epub" else "txt"
    return SourceText(candidate=candidate, format=fmt, path=dest)
