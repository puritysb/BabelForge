"""Find a book cover image for the EPUB.

Tries Project Gutenberg (when the source is Gutenberg), then Google Books
(best title/author coverage, high-quality thumbnails), then Open Library.
Returns raw JPEG bytes ready to drop into the EPUB, or None if nothing found.
"""
from __future__ import annotations
import os
import sys
import urllib.request
import urllib.parse
from typing import Optional

import requests


def _fetch_image(url: str, timeout: float = 15.0) -> Optional[bytes]:
    try:
        # Google Books / Open Library serve over HTTPS; force upgrade.
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "OpenClaw-book_translator/0.1"})
        if r.status_code == 200 and len(r.content) > 1024:
            ctype = r.headers.get("content-type", "").lower()
            if "image" in ctype or url.lower().endswith((".jpg", ".jpeg", ".png")):
                return r.content
    except requests.RequestException:
        pass
    return None


def from_gutenberg(gutenberg_id) -> Optional[bytes]:
    """Project Gutenberg ships a cover for almost every book."""
    gid = int(gutenberg_id)
    for url in (
        f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.cover.medium.jpg",
        f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.cover.small.jpg",
    ):
        data = _fetch_image(url)
        if data:
            return data
    return None


def from_google_books(title: str, author: str = "") -> Optional[bytes]:
    """Google Books volume search → imageLinks. Good coverage, decent quality."""
    q = f"intitle:{title!r}" if title else ""
    if author:
        q += ("" if not q else " ") + f"inauthor:{author!r}"
    try:
        r = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": q or title, "maxResults": 5, "printType": "books"},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
    except (requests.RequestException, ValueError):
        return None
    for item in items:
        links = (item.get("volumeInfo") or {}).get("imageLinks") or {}
        # Pick the highest-resolution variant available.
        for key in ("extraLarge", "large", "medium", "thumbnail", "smallThumbnail"):
            url = links.get(key)
            if url:
                data = _fetch_image(url)
                if data:
                    return data
    return None


def from_open_library(title: str, author: str = "") -> Optional[bytes]:
    """Open Library search → cover_i → covers.openlibrary.org."""
    try:
        r = requests.get(
            "https://openlibrary.org/search.json",
            params={"title": title, "author": author, "limit": 5},
            timeout=10,
        )
        r.raise_for_status()
        docs = r.json().get("docs", [])
    except (requests.RequestException, ValueError):
        return None
    for doc in docs:
        cover_i = doc.get("cover_i")
        if not cover_i:
            continue
        for size in ("L", "M", "S"):
            data = _fetch_image(f"https://covers.openlibrary.org/b/id/{cover_i}-{size}.jpg")
            if data:
                return data
    return None


def find_cover(title: str, author: str = "",
               source: str = "", source_id: str = "",
               extra: dict | None = None) -> Optional[bytes]:
    """Try sources in best-first order. Returns JPEG/PNG bytes or None."""
    extra = extra or {}
    # 1. Project Gutenberg direct (cheapest + authoritative for PG books).
    if source == "gutenberg":
        gid = extra.get("gutenberg_id") or source_id
        data = from_gutenberg(gid)
        if data:
            return data
    # 2. Google Books (best general coverage).
    data = from_google_books(title, author)
    if data:
        return data
    # 3. Open Library fallback.
    data = from_open_library(title, author)
    if data:
        return data
    return None


def main():
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("title")
    ap.add_argument("author", nargs="?", default="")
    ap.add_argument("--source", default="")
    ap.add_argument("--source-id", default="")
    ap.add_argument("--out", help="write image to this path instead of stdout info")
    args = ap.parse_args()
    data = find_cover(args.title, args.author, args.source, args.source_id)
    if not data:
        print("not found", file=sys.stderr)
        sys.exit(1)
    print(f"found {len(data)} bytes", file=sys.stderr)
    if args.out:
        with open(args.out, "wb") as f:
            f.write(data)
        print(args.out)
    else:
        sys.stdout.buffer.write(data)


if __name__ == "__main__":
    main()
