from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pipeline
from extract import Chapter
from translate import TranslatedBook, TranslatedChapter


class PipelinePrecisionTests(unittest.TestCase):
    def test_precision_stages_complete_before_publish(self):
        candidate = {
            "source": "gutenberg", "source_id": "1", "title": "Test",
            "author": "Author", "language": "en", "year": None,
            "rights": "public domain", "fetch_url": "https://example/book.txt",
            "extra": {},
        }
        source_research = {
            "source_language": "en", "original_language": "en",
            "source_is_translation": False, "confidence": "high",
        }
        sheet = {
            "schema_version": 1, "profile_version": "precision-v1",
            "source_hash": "hash", "profile_hash": "profile",
            "book": {"title": "Test", "author": "Author",
                     "source_language": "en"},
            "concepts": [], "source_research": source_research,
            "research": {"web_evidence": []},
        }
        book = TranslatedBook(
            title="Test", author="Author", quality_report={"passed": True},
            chapters=[TranslatedChapter(title="One", pairs=[("Hello.", "안녕.")])],
        )
        updates = []
        pushed = SimpleNamespace(
            pushed=True, skipped=False, path="/Books", elapsed_ms=1,
            to_dict=lambda: {"pushed": True},
        )

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(pipeline.config, "CONCEPT_DIR", temp_dir), \
                patch.object(pipeline.config, "CHECKPOINT_DIR", temp_dir), \
                patch.object(pipeline.catalog, "get", return_value={}), \
                patch.object(pipeline.catalog, "update",
                             side_effect=lambda req_id, **fields: updates.append(fields) or fields), \
                patch.object(pipeline.catalog, "set_progress"), \
                patch.object(pipeline.catalog, "mark_published"), \
                patch.object(pipeline, "research_source", return_value=source_research), \
                patch.object(pipeline, "choose_original_candidate",
                             side_effect=lambda cand, research, fn: (cand.to_dict(), False)), \
                patch.object(pipeline, "build_concept_sheet", return_value=sheet), \
                patch.object(pipeline, "persist_concept_sheet"), \
                patch.object(pipeline, "flat_glossary", return_value={}), \
                patch.object(pipeline, "get_adapter") as get_adapter, \
                patch.object(pipeline, "extract",
                             return_value=[Chapter(title="One", paragraphs=["Hello."])]), \
                patch.object(pipeline, "translate_book", return_value=book), \
                patch.object(pipeline.cover_mod, "find_cover", return_value=None), \
                patch.object(pipeline, "assemble_epub", return_value="/tmp/test.epub"), \
                patch.object(pipeline, "publish_epub", return_value=(1, "https://opds/1")), \
                patch.object(pipeline, "push_to_device", return_value=pushed):
            get_adapter.return_value.fetch.return_value = SimpleNamespace()
            result = pipeline.run(candidate, req_id="req", notify=False)

        self.assertTrue(result["ok"])
        statuses = [row["status"] for row in updates if "status" in row]
        self.assertEqual(
            ["researching", "selected", "analyzing", "translating",
             "quality_checked", "assembling"],
            statuses,
        )


if __name__ == "__main__":
    unittest.main()
