from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import config
from extract import _extract_txt


class TextExtractionTests(unittest.TestCase):
    def test_indented_german_contents_restores_real_chapters(self):
        first = "\n\n".join(
            f"Vorrede Absatz {index} mit genügend Text."
            for index in range(1, 10))
        second = "\n\n".join(
            f"Philosophischer Absatz {index} mit genügend Text."
            for index in range(1, 10))
        source = f"""*** START OF THE PROJECT GUTENBERG EBOOK TEST ***

Inhalt

    An Stelle einer Vorrede
    Von den ersten und letzten Dingen
    Ein Nachspiel

An Stelle einer Vorrede.

{first}

Von den ersten und letzten Dingen.

{second}

Ein Nachspiel.

Letzter Absatz.

*** END OF THE PROJECT GUTENBERG EBOOK TEST ***
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "book.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)
            with patch.object(config, "SENTENCE_LEVEL_SPLIT", False):
                chapters = _extract_txt(path)

        self.assertEqual([
            "An Stelle einer Vorrede.",
            "Von den ersten und letzten Dingen.",
            "Ein Nachspiel.",
        ], [chapter.title for chapter in chapters])
        self.assertEqual(
            "An Stelle einer Vorrede.", chapters[0].paragraphs[0])
        self.assertNotIn("Untitled", " ".join(ch.title for ch in chapters))


if __name__ == "__main__":
    unittest.main()
