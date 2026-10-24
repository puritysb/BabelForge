from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import config
from sources import Candidate
from sources import gutenberg, standard_ebooks, annas_archive
from sources.download import cached_source, detect_format, store_response


def response(data: bytes, content_type="text/plain", url="https://example.test/book"):
    return SimpleNamespace(content=data, headers={"content-type": content_type},
                           text=data.decode("utf-8", errors="replace"), url=url,
                           raise_for_status=lambda: None)


def epub_bytes(marker="book"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", "<container/>")
        archive.writestr("marker.txt", marker)
    return stream.getvalue()


class SourceDownloadTests(unittest.TestCase):
    def test_later_download_never_changes_source_already_returned_to_worker(self):
        candidate = Candidate("gutenberg", "1", "Book", "Author", fetch_url="https://example.test/book.txt")
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "CACHE_DIR", directory):
            first = store_response(candidate, response(b"First retained edition"))
            second = store_response(candidate, response(b"Updated remote edition"))
            self.assertNotEqual(first.path, second.path)
            self.assertEqual(b"First retained edition", Path(first.path).read_bytes())
            self.assertEqual(second.path, cached_source(candidate).path)
    def test_gutenberg_txt_in_epub_directory_is_text(self):
        candidate = Candidate("gutenberg", "1", "Book", "Author", fetch_url="https://www.gutenberg.org/cache/epub/1/pg1.txt")
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "CACHE_DIR", directory):
            with patch.object(gutenberg.SESSION, "get", return_value=response(b"CHAPTER I\n\nText.")):
                result = gutenberg.fetch(candidate)
            self.assertEqual("txt", result.format)
            self.assertEqual("txt", cached_source(candidate).format)

    def test_signatures_override_generic_mime_and_html_is_explicit(self):
        self.assertEqual("epub", detect_format(epub_bytes(), "application/octet-stream"))
        self.assertEqual("html", detect_format(b"<html><body>Text</body></html>", "text/html"))
        with self.assertRaises(ValueError):
            detect_format(b"<html>Error page</html>", "application/epub+zip")
        with self.assertRaises(ValueError):
            detect_format(b"PKbad zip")
        with self.assertRaises(ValueError):
            detect_format(epub_bytes("CRC marker").replace(b"CRC marker", b"BAD marker"))
        with self.assertRaises(ValueError):
            detect_format(b"%PDF-1.4", "application/pdf")

    def test_corrupt_cached_file_is_downloaded_again(self):
        candidate = Candidate("gutenberg", "1", "Book", "Author", fetch_url="https://example.test/book.txt")
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "CACHE_DIR", directory):
            with patch.object(gutenberg.SESSION, "get", return_value=response(b"Complete source")) as request:
                first = gutenberg.fetch(candidate)
                with open(first.path, "wb") as handle:
                    handle.write(b"Partial")
                second = gutenberg.fetch(candidate)
            self.assertEqual(2, request.call_count)
            with open(second.path, "rb") as handle:
                self.assertEqual(b"Complete source", handle.read())

    def test_download_metadata_is_not_committed_when_atomic_write_fails(self):
        from sources import download
        candidate = Candidate("gutenberg", "1", "Book", "Author", fetch_url="https://example.test/book.txt")
        original = download._atomic_write
        def interrupted(path, data):
            if path.endswith("source.json"):
                raise OSError("simulated interruption")
            return original(path, data)
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "CACHE_DIR", directory):
            with patch.object(download, "_atomic_write", side_effect=interrupted):
                with self.assertRaises(OSError):
                    store_response(candidate, response(b"Complete source"))
            self.assertIsNone(cached_source(candidate))

    def test_standard_ebooks_translated_edition_is_found(self):
        html = b'<a href="/ebooks/leo-tolstoy/war-and-peace/louise-maude_aylmer-maude">War and Peace</a>'
        with patch.object(standard_ebooks.SESSION, "get", return_value=response(html, "text/html")):
            candidates = standard_ebooks.search("War and Peace")
        self.assertEqual(1, len(candidates))
        self.assertEqual("leo-tolstoy/war-and-peace/louise-maude_aylmer-maude", candidates[0].source_id)

    def test_standard_ebooks_identical_title_different_author_do_not_collide(self):
        a = Candidate("standard_ebooks", "poems", "Poems", "Author A", fetch_url="https://standardebooks.org/ebooks/a/poems")
        b = Candidate("standard_ebooks", "poems", "Poems", "Author B", fetch_url="https://standardebooks.org/ebooks/b/poems")
        page = response(b'<a href="downloads/book.epub">Download</a>', "text/html")
        payloads = [page, response(epub_bytes("A"), "application/epub+zip"), page, response(epub_bytes("B"), "application/epub+zip")]
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "CACHE_DIR", directory):
            with patch.object(standard_ebooks.SESSION, "get", side_effect=payloads) as request:
                first = standard_ebooks.fetch(a)
                second = standard_ebooks.fetch(b)
                again = standard_ebooks.fetch(a)
            self.assertNotEqual(first.path, second.path)
            self.assertEqual(first.path, again.path)
            self.assertEqual(4, request.call_count)
            self.assertEqual("https://standardebooks.org/ebooks/a/downloads/book.epub", request.call_args_list[1].args[0])
            with zipfile.ZipFile(second.path) as archive:
                self.assertEqual(b"B", archive.read("marker.txt"))

    def test_rejected_html_does_not_poison_epub_cache(self):
        candidate = Candidate("standard_ebooks", "a/book", "Book", "A", fetch_url="https://example.test/a/book")
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "CACHE_DIR", directory):
            with self.assertRaises(ValueError):
                store_response(candidate, response(b"<html>Bad gateway</html>", "text/html"), allowed_formats={"epub"})
            self.assertIsNone(cached_source(candidate))

    def test_gutenberg_prefers_supported_structured_edition(self):
        data = {"results": [{"id": 1, "title": "Book", "authors": [{"name": "Author"}], "languages": ["en"], "copyright": False,
                             "formats": {"text/plain; charset=utf-8": "https://example.test/book.txt", "application/epub+zip": "https://example.test/book.epub"}}]}
        api = Mock()
        api.json.return_value = data
        with patch.object(gutenberg.SESSION, "get", return_value=api):
            candidate = gutenberg.search("Book")[0]
        self.assertEqual("https://example.test/book.epub", candidate.fetch_url)
        self.assertEqual("application/epub+zip", candidate.extra["media_type"])

    def test_local_epub_uses_edition_metadata_and_language(self):
        from ebooklib import epub
        from sources import local_file
        book = epub.EpubBook()
        book.set_identifier("local-language")
        book.set_title("Die Verwandlung")
        book.set_language("de")
        book.add_author("Franz Kafka")
        chapter = epub.EpubHtml(title="Kapitel", file_name="chapter.xhtml", content="<p>Als Gregor Samsa...</p>")
        book.add_item(chapter)
        book.add_item(epub.EpubNav())
        book.spine = [chapter]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "download-123.epub")
            epub.write_epub(path, book)
            candidate = local_file.search(path)[0]
        self.assertEqual("Die Verwandlung", candidate.title)
        self.assertEqual("Franz Kafka", candidate.author)
        self.assertEqual("de", candidate.language)

    def test_anna_mirror_fallback_resolves_relative_link_and_caches_book(self):
        candidate = Candidate("annas_archive", "fixture-md5", "Book", "Author", fetch_url="https://annas.example/md5/fixture-md5")
        payloads = [
            response(b'<a href="/slow_download/fail">First</a><a href="/slow_download/good">Second</a>', "text/html"),
            response(b'<html><body>Unavailable</body></html>', "text/html"),
            response(b'<a href="/files/book.epub">Download</a>', "text/html", url="https://annas.example/slow_download/good"),
            response(epub_bytes(), "application/epub+zip"),
        ]
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "CACHE_DIR", directory), patch.object(annas_archive, "BASE", "https://annas.example"), patch.dict(annas_archive._LINK_CACHE, {}, clear=True):
            with patch.object(annas_archive.SESSION, "get", side_effect=payloads) as request:
                first = annas_archive.fetch(candidate)
                again = annas_archive.fetch(candidate)
            self.assertEqual("epub", first.format)
            self.assertEqual(first.path, again.path)
            self.assertEqual(4, request.call_count)
            self.assertEqual("https://annas.example/files/book.epub", request.call_args_list[-1].args[0])
            self.assertEqual("https://annas.example/md5/fixture-md5", candidate.fetch_url)
