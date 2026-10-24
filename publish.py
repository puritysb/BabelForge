"""Add a bilingual EPUB to the Calibre library so Content Server serves it.

`calibredb add` writes into metadata.db; the running calibre-server (launchd
com.local.calibre-server) exposes the new book over OPDS within seconds.

After add, we tag the book so it's easy to find from the device's OPDS browser
and return the public OPDS acquisition URL.
"""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
import sys
from contextvars import ContextVar

import config
from state_store import file_lock

calibre_target = ContextVar("calibre_publication_target", default=None)


def db_command(*args):
    return [config.CALIBREDB, "--with-library",
            calibre_target.get() or config.CALIBREDB_URL, *args]


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


def edition_identifier(epub_path: str, source: str, source_id: str,
                       source_lang: str) -> str:
    """Identify an edition independently of title, author, or translation retry."""
    if source_id:
        identity = json.dumps([source, str(source_id), source_lang, "ko"],
                              ensure_ascii=False).encode("utf-8")
    else:
        # Legacy CLI callers have no source identity. A content hash is safer
        # than merging unrelated editions because their titles happen to match.
        with open(epub_path, "rb") as f:
            identity = f.read()
    return hashlib.sha256(identity).hexdigest()


def _matching_ids(identifier: str) -> list[int]:
    # Preserve the earlier --search/--for-machine fix: never infer identity
    # from a positional query or the newest book in the whole library.
    listing = _run(db_command(
        "list", "--for-machine", "--fields", "identifiers",
        "--search", f'identifiers:="babelforge:{identifier}"'))
    rows = json.loads(listing)
    if not isinstance(rows, list):
        raise RuntimeError("Calibre returned an invalid book listing")
    return [row["id"] for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), int)
            and not isinstance(row["id"], bool)
            and isinstance(row.get("identifiers"), dict)
            and row["identifiers"].get("babelforge") == identifier]


def add_to_library(epub_path: str, title: str, author: str,
                   source: str = "", rights: str = "", *,
                   source_id: str = "", source_lang: str = "en",
                   preferred_id: int | None = None) -> int:
    """Upsert one identified edition without deleting an existing book record."""
    if not os.path.isfile(epub_path):
        raise FileNotFoundError(epub_path)
    identifier = edition_identifier(epub_path, source, source_id, source_lang)
    languages = ",".join(dict.fromkeys(["ko", source_lang or "en"]))
    tags = "bilingual,korean,babelforge"
    if source:
        tags += f",source:{source}"
    with file_lock(os.path.join(config.DATA_DIR, ".publish.lock")):
        ids = _matching_ids(identifier)
        if preferred_id is not None:
            if preferred_id not in ids:
                raise RuntimeError("Preferred record has no matching edition identity")
            ids = [preferred_id]
        if len(ids) > 1:
            raise RuntimeError(f"Multiple Calibre records share edition {identifier}")
        if ids:
            book_id = ids[0]
            # add_format preserves the book record and stable acquisition URL.
            # No remove command is used, even if the replacement fails.
            _run(db_command("add_format", str(book_id), epub_path), timeout=120)
            _run(db_command(
                "set_metadata", str(book_id), "--field", f"title:{title}",
                "--field", f"authors:{author or 'Unknown'}",
                "--field", f"languages:{languages}", "--field", f"tags:{tags}"))
            return book_id
        _run(db_command(
            "add", "--duplicates", "--title", title, "--authors", author or "Unknown",
            "--tags", tags, "--languages", languages,
            "--identifier", f"babelforge:{identifier}", epub_path), timeout=120)
        # Localized CLI output and concurrent unrelated imports cannot return
        # another book's ID: resolve only the identifier just written.
        ids = _matching_ids(identifier)
        if len(ids) != 1:
            raise RuntimeError("Could not uniquely verify the imported Calibre edition")
        return ids[0]


def opds_url_for(book_id: int) -> str:
    """Return a verified Calibre Content Server EPUB acquisition URL.

    Current Calibre versions do not expose the historical ``/opds/book/<id>``
    route (it returns 404). OPDS feeds themselves remain rooted at ``/opds``;
    the stable acquisition target advertised by the server is ``/get/EPUB/N``.
    """
    return f"{config.OPDS_BASE_URL}/get/EPUB/{book_id}"


def publish(epub_path: str, title: str, author: str,
            source: str = "", rights: str = "", *,
            source_id: str = "", source_lang: str = "en",
            request_id: str | None = None, **revision_options) -> tuple[int, str]:
    from publication import publish_revision
    result = publish_revision(epub_path, title, author, source=source, rights=rights,
        source_id=source_id, source_lang=source_lang, request_id=request_id,
        **revision_options)
    return result["book_id"], result["opds_url"]


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
