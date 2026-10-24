import unittest
from unittest.mock import Mock, patch

import requests

import cover


def response(content, content_type="application/octet-stream", status=200):
    return Mock(status_code=status, content=content,
                headers={"content-type": content_type})


class CoverImageTests(unittest.TestCase):
    def test_supported_formats_are_identified_by_bytes(self):
        for signature in (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a"):
            with self.subTest(signature=signature):
                data = signature + b"x" * 1100
                with patch.object(cover.requests, "get", return_value=response(data)):
                    self.assertEqual(data, cover._fetch_image("https://example.org/cover"))

    def test_image_header_and_jpeg_url_do_not_approve_html(self):
        data = b"<!DOCTYPE html>" + b"error" * 300
        for content_type in ("text/html", "image/jpeg"):
            with self.subTest(content_type=content_type), \
                    patch.object(cover.requests, "get", return_value=response(data, content_type)):
                self.assertIsNone(cover._fetch_image("https://example.org/cover.jpg"))

    def test_unsupported_image_formats_return_none(self):
        for data, content_type in ((b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 1100, "image/webp"),
                                   (b"<svg xmlns='http://www.w3.org/2000/svg'>" + b"x" * 1100, "image/svg+xml")):
            with self.subTest(content_type=content_type), \
                    patch.object(cover.requests, "get", return_value=response(data, content_type)):
                self.assertIsNone(cover._fetch_image("https://example.org/cover.jpg"))

    def test_invalid_first_cover_falls_back_to_next_candidate(self):
        valid = b"\x89PNG\r\n\x1a\n" + b"x" * 1100
        responses = [response(b"<html>error</html>" * 100, "image/jpeg"), response(valid)]
        with patch.object(cover.requests, "get", side_effect=responses) as get:
            self.assertEqual(valid, cover.from_gutenberg("123"))
        self.assertEqual(2, get.call_count)
        self.assertIn("cover.small.jpg", get.call_args.args[0])

    def test_failed_or_tiny_response_is_ignored(self):
        for result in (response(b"\xff\xd8\xff", "image/jpeg"),
                       response(b"\xff\xd8\xff" + b"x" * 1100, "image/jpeg", status=404)):
            with patch.object(cover.requests, "get", return_value=result):
                self.assertIsNone(cover._fetch_image("https://example.org/cover.jpg"))
        with patch.object(cover.requests, "get", side_effect=requests.Timeout("test")):
            self.assertIsNone(cover._fetch_image("https://example.org/cover.jpg"))


if __name__ == "__main__":
    unittest.main()
