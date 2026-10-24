"""Anna's Archive adapter — opt-in only.

Copyright risk: many entries are in-copyright in most jurisdictions. This
adapter is invoked only when the user explicitly prefixes the request with
`annas:` (see search.py routing). Auto-suggest never uses it.

Implementation scrapes the public search HTML. Mirror availability shifts;
on repeated failure, the user should retry with a different network.
"""
import re
import requests
from bs4 import BeautifulSoup
from sources import Candidate, SourceText
from sources.download import cached_source, store_response
from urllib.parse import urljoin
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
    seen: set[str] = set()
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
        if md5 in seen:
            continue
        seen.add(md5)
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
    """Resolve an explicit opt-in request, rejecting intermediary HTML pages."""
    identity = f"{BASE}/md5/{candidate.source_id}"
    cached = cached_source(candidate, identity)
    if cached is not None:
        return cached
    direct = _LINK_CACHE.get(candidate.source_id) or candidate.fetch_url
    links = []
    if direct and "/md5/" not in direct:
        links.append(direct)
    else:
        response = SESSION.get(identity, timeout=25)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        links = list(dict.fromkeys(urljoin(identity, a["href"]) for a in
                     soup.select("a[href*='slow_download']") if a.get("href")))
    errors = []
    for link in links:
        try:
            response = SESSION.get(link, timeout=120)
            response.raise_for_status()
            if "html" in response.headers.get("content-type", "").lower():
                soup = BeautifulSoup(response.text, "lxml")
                anchor = soup.select_one(
                    "a[href*='.epub'], a[href*='.txt'], a[download]")
                if not anchor or not anchor.get("href"):
                    raise ValueError("download mirror returned an intermediary page")
                response = SESSION.get(urljoin(response.url, anchor["href"]), timeout=120)
                response.raise_for_status()
            result = store_response(candidate, response, identity,
                                    allowed_formats={"epub", "txt"})
            _LINK_CACHE[candidate.source_id] = link
            return result
        except (requests.RequestException, ValueError, UnicodeError) as error:
            errors.append(str(error))
    detail = errors[-1] if errors else "no supported download link found"
    raise RuntimeError(f"could not download {candidate.source_id}: {detail}")
