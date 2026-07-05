"""Anna's Archive adapter — opt-in only.

Copyright risk: many entries are in-copyright in most jurisdictions. This
adapter is invoked only when the user explicitly prefixes the request with
`annas:` (see search.py routing). Auto-suggest never uses it.

Implementation scrapes the public search HTML. Mirror availability shifts;
on repeated failure, the user should retry with a different network.
"""
import os
import re
import requests
from bs4 import BeautifulSoup
from sources import Candidate, SourceText
import config

BASE = config.ANNAS_ARCHIVE_BASE
SESSION = requests.Session()
SESSION.headers["User-Agent"] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Track scraped download links per source_id so fetch() can use them without
# re-scraping. Cleared on process restart.
_LINK_CACHE: dict[str, str] = {}


def search(query: str, limit: int = config.MAX_CANDIDATES) -> list[Candidate]:
    out: list[Candidate] = []
    r = SESSION.get(f"{BASE}/search", params={"q": query}, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    for div in soup.select("div:has(> a[href*='/md5/']), a[href*='/md5/']"):
        a = div if div.name == "a" else div.select_one("a[href*='/md5/']")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"/md5/([0-9a-fA-F]+)", href)
        if not m:
            continue
        md5 = m.group(1).lower()
        # Title/author: try neighboring text nodes; AA's markup is volatile,
        # so be defensive. Prefer the link's visible text as a fallback title.
        label = a.get_text(" ", strip=True)
        title = label or f"anna-{md5[:8]}"
        author = "Unknown"
        # AA listings sometimes append " -- Author, Year"
        am = re.search(r"\bby\b\s+(.+?)$|-\s*([^,]+?),?\s*(\d{4})", label)
        if am:
            author = (am.group(1) or am.group(2) or author).strip()
        out.append(Candidate(
            source="annas_archive",
            source_id=md5,
            title=title[:160],
            author=author,
            language="en",
            rights="unknown (user responsibility)",
            fetch_url=f"{BASE}/md5/{md5}",
            extra={"md5": md5},
        ))
        if len(out) >= limit:
            break
    return out


def fetch(candidate: Candidate) -> SourceText:
    """Resolve a slow_download or mirror link from the MD5 page and fetch.

    AA exposes multiple mirrors per book; we try them in order until one
    yields a file we can save. Files may be .epub, .pdf, or .txt — only .epub
    and .txt are useful for our reader.
    """
    if candidate.source_id in _LINK_CACHE:
        candidate.fetch_url = _LINK_CACHE[candidate.source_id]

    cache_dir = os.path.join(config.CACHE_DIR,
                             f"{candidate.source}-{candidate.source_id}")
    os.makedirs(cache_dir, exist_ok=True)

    # If we already have a direct link, just download.
    direct = candidate.fetch_url
    if not direct or "/md5/" in direct:
        r = SESSION.get(f"{BASE}/md5/{candidate.source_id}", timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        # Look for slow/free mirror links; AA marks them with /slow_download/
        for a in soup.select("a[href*='/slow_download/'], a[href*='slow_download']"):
            href = a.get("href", "")
            if href.startswith("/"):
                href = BASE + href
            candidate.fetch_url = href
            _LINK_CACHE[candidate.source_id] = href
            break

    # Now fetch the actual file through the slow_download redirect chain.
    if not candidate.fetch_url or "/md5/" in candidate.fetch_url:
        raise RuntimeError(f"could not resolve mirror for {candidate.source_id}")

    # slow_download returns an intermediate page; follow to the file.
    r = SESSION.get(candidate.fetch_url, timeout=30)
    r.raise_for_status()
    final_url = r.url
    # Heuristically detect file extension from the final URL or content-type.
    ext = ".epub"
    if final_url.lower().endswith(".pdf") or "pdf" in r.headers.get("content-type", ""):
        ext = ".pdf"
    elif final_url.lower().endswith(".txt") or "text/plain" in r.headers.get("content-type", ""):
        ext = ".txt"
    elif final_url.lower().endswith(".epub") or "epub" in r.headers.get("content-type", ""):
        ext = ".epub"

    dest = os.path.join(cache_dir, f"book{ext}")
    # If slow_download returned HTML, parse out the final link.
    if "html" in r.headers.get("content-type", "").lower():
        soup = BeautifulSoup(r.text, "lxml")
        a = soup.select_one("a[href*='.epub'], a[href*='.pdf'], a[href*='.txt'], a[download]")
        if a and a.get("href"):
            href = a["href"]
            if href.startswith("/"):
                href = BASE + href
            r = SESSION.get(href, timeout=120)
            r.raise_for_status()
            ext = ".epub" if ".epub" in href else (".pdf" if ".pdf" in href else ".txt")
            dest = os.path.join(cache_dir, f"book{ext}")
    with open(dest, "wb") as f:
        f.write(r.content)

    fmt = "epub" if ext == ".epub" else ("txt" if ext == ".txt" else "pdf")
    if fmt == "pdf":
        raise RuntimeError(
            f"{candidate.source_id} resolved to PDF — book_translator only "
            f"supports EPUB/txt. Try another source or run ebook-convert first."
        )
    return SourceText(candidate=candidate, format=fmt, path=dest)
