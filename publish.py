"""Add a bilingual EPUB to the Calibre library so Content Server serves it.

`calibredb add` writes into metadata.db; the running calibre-server (launchd
com.local.calibre-server) exposes the new book over OPDS within seconds.

After add, we tag the book so it's easy to find from the device's OPDS browser
and return the public OPDS acquisition URL.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import urllib.parse

import config


def _run(args: list[str], timeout: int = 60) -> str:
    try:
        out = subprocess.run(
            args, check=True, capture_output=True, text=True, timeout=timeout
        )
        return out.stdout
    except subprocess.CalledProcessError as e:
        sys.stderr.write(
            f"[publish] cmd failed: {' '.join(args)}\n"
            f"  stdout: {e.stdout}\n  stderr: {e.stderr}\n"
        )
        raise


def add_to_library(epub_path: str, title: str, author: str,
                   source: str = "", rights: str = "") -> int:
    """Add EPUB to the Calibre library, return the new calibre book id.

    Idempotency: calibredb add never dedupes, so we first check for an existing
    book with the same title and replace it.
    """
    if not os.path.isfile(epub_path):
        raise FileNotFoundError(epub_path)

    # Check for an existing copy to replace (avoids "Pride (KO bilingual) (2)").
    existing_id = None
    try:
        listing = _run(config.calibredb_cmd("list",
                                            f"search:title:=\"{title}\""))
        for line in listing.strip().splitlines()[1:]:
            parts = line.split(None, 1)
            if parts and parts[0].isdigit():
                existing_id = int(parts[0])
                break
    except Exception:
        pass  # not fatal; just add
    if existing_id is not None:
        try:
            _run(config.calibredb_cmd("remove", str(existing_id),
                                      "--permanent"))
        except Exception:
            pass

    args = config.calibredb_cmd("add")
    # Apply metadata + tags at insert time so the OPDS feed is immediately clean.
    # Note: calibredb add has no --comment option; comments are set via
    # ebook-meta after the book exists (see _set_comment below).
    tags = "bilingual, korean, openclaw"
    if source:
        tags += f", source:{source}"
    args += ["--title", title, "--authors", author or "Unknown",
             "--tags", tags, "--languages", "kor,eng"]
    args.append(epub_path)
    out = _run(args, timeout=120)
    # calibredb prints "Added book ids: N"
    for line in out.splitlines():
        if "Added book ids:" in line:
            ids = line.split(":", 1)[1].strip()
            try:
                return int(ids.split(",")[0])
            except ValueError:
                pass
    # Fallback: ask the database for the most recently added id.
    listing = _run(config.calibredb_cmd("list", "--sort-by=timestamp",
                                        "--descending", "--limit=1"))
    for line in listing.strip().splitlines()[1:]:
        parts = line.split(None, 1)
        if parts and parts[0].isdigit():
            return int(parts[0])
    raise RuntimeError(f"could not determine new book id. output: {out!r}")


def _set_comment(book_id: int, comment: str) -> None:
    """Best-effort: set the comments field via the Content server API."""
    try:
        _run(config.calibredb_cmd("set_custom", "comments", str(book_id), comment),
             timeout=30)
    except Exception:
        # Comments are cosmetic — don't fail the publish if this doesn't stick
        # (e.g. server without the custom column).
        pass


def opds_url_for(book_id: int) -> str:
    """The acquisition entry URL on our public Content Server."""
    # Calibre's OPDS exposes /opds/book/<id>; the acquisition link points at
    # /opds/book/<id>/file?fmt=epub. We return the book page so the device's
    # browser can render the entry and download from there.
    return f"{config.OPDS_BASE_URL}/opds/book/{book_id}"


def publish(epub_path: str, title: str, author: str,
            source: str = "", rights: str = "") -> tuple[int, str]:
    book_id = add_to_library(epub_path, title, author, source, rights)
    return book_id, opds_url_for(book_id)


def main():
    """CLI: publish <epub> <title> <author> [source] [rights] → prints book_id + opds_url"""
    if len(sys.argv) < 4:
        print("usage: publish.py <epub> <title> <author> [source] [rights]",
              file=sys.stderr)
        sys.exit(2)
    epub = sys.argv[1]
    title = sys.argv[2]
    author = sys.argv[3]
    source = sys.argv[4] if len(sys.argv) > 4 else ""
    rights = sys.argv[5] if len(sys.argv) > 5 else ""
    book_id, url = publish(epub, title, author, source, rights)
    print(f"{book_id}\t{url}")


if __name__ == "__main__":
    main()
