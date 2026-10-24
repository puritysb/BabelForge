"""Project Gutenberg adapter via the gutendex API.

Public domain. The safest default source for auto-suggest.
"""
import requests
from sources import Candidate, SourceText
from sources.download import download_source
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
            # Prefer the structured edition, retaining the selected MIME type.
            fmt = b.get("formats", {})
            media_type, fetch_url = next(
                ((mime, fmt[mime]) for mime in (
                    "application/epub+zip", "text/plain; charset=utf-8",
                    "text/plain; charset=us-ascii", "text/plain",
                    "text/html; charset=utf-8", "text/html") if fmt.get(mime)),
                (None, None))
            if not fetch_url:
                continue
            out.append(Candidate(
                source="gutenberg",
                source_id=str(b["id"]),
                title=b.get("title", "").strip(),
                author=authors,
                language=langs[0] if langs else "en",
                rights=rights,
                fetch_url=fetch_url,
                extra={"gutenberg_id": b["id"], "media_type": media_type},
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
    return download_source(candidate, SESSION, candidate.fetch_url)
