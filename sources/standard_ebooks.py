"""Standard Ebooks adapter.

The SE OPDS feed requires a member token/IP allow-list and 401s from some
networks, so we fall back to the public website search which returns the same
metadata. Downloads are EPUB-3, well-typeset, public domain.
"""
import re
import warnings
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from sources import Candidate, SourceText
from sources.download import cached_source, store_response
import config

BASE = "https://standardebooks.org"
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "OpenClaw-book_translator/0.1 (+local)"
# SE serves XHTML; silence the "XML parsed as HTML" noise.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


def search(query: str, limit: int = config.MAX_CANDIDATES) -> list[Candidate]:
    """Scrape the public ebook search results page.

    SE book pages have the path /ebooks/<author-slug>/<title-slug>; author
    index pages are /ebooks/<author-slug> (shorter), so we filter on segment
    count to keep only book links.
    """
    out: list[Candidate] = []
    r = SESSION.get(f"{BASE}/ebooks", params={"query": query}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    seen_paths: set[str] = set()
    from urllib.parse import urlparse
    for a in soup.select('a[href*="/ebooks/"]'):
        raw = a.get("href", "")
        # Normalize: handle both absolute (https://standardebooks.org/...)
        # and relative (/ebooks/...) links.
        path = urlparse(raw).path
        if not path.startswith("/ebooks/"):
            continue
        href = path.split("?")[0].split("#")[0]
        segs = [s for s in href.strip("/").split("/") if s]
        # Translated editions append one or more contributor slugs. Exclude
        # asset/reading links without excluding those legitimate editions.
        if len(segs) < 3 or segs[0] != "ebooks" or any(
                segment in {"downloads", "text", "images", "css"} or "." in segment
                for segment in segs[3:]):
            continue
        if href in seen_paths:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            # Likely an image/cover link with the same href as a later text
            # link. Skip WITHOUT marking seen so the text link is picked up.
            continue
        seen_paths.add(href)
        author_slug, title_slug = segs[1], segs[2]
        author = author_slug.replace("-", " ").title()
        out.append(Candidate(
            source="standard_ebooks",
            source_id="/".join(segs[1:]),
            title=title[:200],
            author=author,
            language="en",
            rights="public domain",
            fetch_url=f"{BASE}{href}",   # book page; fetch() resolves the epub
            extra={"page_url": f"{BASE}{href}",
                   "author_slug": author_slug, "title_slug": title_slug},
        ))
        if len(out) >= limit:
            break
    return out


def fetch(candidate: Candidate) -> SourceText:
    """Visit the book page, extract the compatible-epub download link, fetch.

    SE's compatible epub is the one our reader handles cleanly (reflowable,
    well-typeset). Pattern:
      /ebooks/<author>/<title>/downloads/<author>_<title>.epub
    """
    from urllib.parse import urljoin, urlparse
    page_url = (candidate.extra or {}).get("page_url") or candidate.fetch_url
    if not page_url:
        raise ValueError(f"no page url for {candidate}")
    cached = cached_source(candidate, page_url)
    if cached is not None:
        return cached
    # Older saved candidates may already contain the resolved download URL.
    if urlparse(page_url).path.endswith(".epub"):
        r = SESSION.get(page_url, timeout=120)
        r.raise_for_status()
        return store_response(candidate, r, page_url, allowed_formats={"epub"})
    r = SESSION.get(page_url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    epub_url = None
    for a in soup.select('a[href$=".epub"]'):
        href = a.get("href", "")
        # Skip "advanced" / "kepub" variants — pick the plain compatible epub.
        if "advanced" in href or "kepub" in href:
            continue
        epub_url = urljoin(page_url, href)
        break
    if not epub_url:
        raise RuntimeError(f"no epub link on {page_url}")
    r = SESSION.get(epub_url, timeout=120)
    r.raise_for_status()
    return store_response(candidate, r, page_url, allowed_formats={"epub"})
