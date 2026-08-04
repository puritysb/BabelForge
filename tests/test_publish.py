from __future__ import annotations

import unittest
from unittest.mock import patch

from publish import opds_url_for


class PublishUrlTests(unittest.TestCase):
    def test_calibre_acquisition_url_uses_live_get_route(self):
        with patch("publish.config.OPDS_BASE_URL", "https://books.example"):
            self.assertEqual(
                "https://books.example/get/EPUB/33",
                opds_url_for(33),
            )


if __name__ == "__main__":
    unittest.main()
