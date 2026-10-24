"""Validated, atomic source downloads shared by the catalog adapters."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import zipfile

import config
from sources import Candidate, SourceText


def detect_format(data: bytes, content_type: str = "") -> str:
    """Use the payload, never a substring of its URL, to identify a book."""
    if not data:
        raise ValueError("downloaded source is empty")
    mime = content_type.split(";", 1)[0].strip().lower()
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if archive.read("mimetype").strip() != b"application/epub+zip":
                    raise ValueError("ZIP source is not an EPUB")
                archive.getinfo("META-INF/container.xml")
                if archive.testzip() is not None:
                    raise ValueError("downloaded EPUB has a corrupt archive member")
        except (zipfile.BadZipFile, KeyError) as error:
            raise ValueError("downloaded EPUB is incomplete or invalid") from error
        return "epub"
    if mime == "application/epub+zip":
        raise ValueError("EPUB response does not contain an EPUB archive")
    if data.lstrip().startswith(b"%PDF") or mime == "application/pdf":
        raise ValueError("PDF sources are unsupported; choose EPUB or text")
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if mime in {"text/html", "application/xhtml+xml"} or re.search(
            br"<(?:!doctype\s+html|html\b)", head):
        return "html"
    if b"\x00" in data or (mime and mime not in {
            "text/plain", "application/octet-stream"}):
        raise ValueError(f"unsupported downloaded source type: {mime or 'binary'}")
    return "txt"


def _cache_dir(candidate: Candidate, identity: str | None = None) -> str:
    identity = identity or candidate.fetch_url or candidate.source_id
    digest = hashlib.sha256(
        f"{candidate.source}\0{identity}".encode("utf-8")).hexdigest()[:24]
    return os.path.join(config.CACHE_DIR, f"source-{digest}")


def cached_source(candidate: Candidate, identity: str | None = None) -> SourceText | None:
    """Accept only complete caches whose bytes still match their metadata."""
    directory = _cache_dir(candidate, identity)
    try:
        with open(os.path.join(directory, "source.json"), encoding="utf-8") as handle:
            metadata = json.load(handle)
        fmt = metadata["format"]
        if fmt not in {"txt", "html", "epub"}:
            return None
        checksum = metadata["sha256"]
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            return None
        filename = metadata.get("filename", f"book.{fmt}")
        if filename not in {f"book.{fmt}", f"book-{checksum}.{fmt}"}:
            return None
        path = os.path.join(directory, filename)
        with open(path, "rb") as handle:
            data = handle.read()
        if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            return None
        if detect_format(data, metadata.get("content_type", "")) != fmt:
            return None
        return SourceText(candidate=candidate, format=fmt, path=path)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _atomic_write(path: str, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def store_response(candidate: Candidate, response, identity: str | None = None,
                   allowed_formats: set[str] | None = None) -> SourceText:
    data = response.content
    content_type = response.headers.get("content-type", "")
    fmt = detect_format(data, content_type)
    if allowed_formats is not None and fmt not in allowed_formats:
        raise ValueError(f"download returned {fmt}, expected {sorted(allowed_formats)}")
    if fmt in {"txt", "html"}:
        match = re.search(r"charset\s*=\s*['\"]?([^;\s'\"]+)", content_type, re.I)
        encoding = match.group(1) if match else "utf-8-sig"
        # Normalize known encodings explicitly; replacement characters would
        # silently corrupt the source before the translation audit can see it.
        data = data.decode(encoding).encode("utf-8")
        content_type = "text/html; charset=utf-8" if fmt == "html" else "text/plain; charset=utf-8"
    directory = _cache_dir(candidate, identity)
    os.makedirs(directory, exist_ok=True)
    checksum = hashlib.sha256(data).hexdigest()
    filename = f"book-{checksum}.{fmt}"
    path = os.path.join(directory, filename)
    _atomic_write(path, data)
    metadata = {"format": fmt, "content_type": content_type,
                "sha256": checksum, "filename": filename}
    _atomic_write(os.path.join(directory, "source.json"),
                  json.dumps(metadata).encode("utf-8"))
    return SourceText(candidate=candidate, format=fmt, path=path)


def download_source(candidate: Candidate, session, url: str,
                    identity: str | None = None) -> SourceText:
    cached = cached_source(candidate, identity)
    if cached is not None:
        return cached
    response = session.get(url, timeout=120)
    response.raise_for_status()
    return store_response(candidate, response, identity)
