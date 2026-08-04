from __future__ import annotations

import unittest
from unittest.mock import patch

import babelforge_mcp


class RetryRequestTests(unittest.TestCase):
    def test_failed_request_reuses_same_id_and_candidate(self):
        candidate = {
            "source": "gutenberg",
            "source_id": "7207",
            "title": "Menschliches, Allzumenschliches",
            "author": "Nietzsche",
            "language": "de",
        }
        record = {
            "id": "req-1",
            "status": "failed",
            "selected": candidate,
            "retry_count": 1,
        }
        spawned = {
            "req_id": "req-1",
            "status": "translating",
            "worker_pid": 123,
        }
        with patch.object(babelforge_mcp.catalog, "get", return_value=record), \
                patch.object(babelforge_mcp.catalog, "update") as update, \
                patch.object(babelforge_mcp, "_spawn_pipeline",
                             return_value=spawned) as spawn:
            result = babelforge_mcp.retry_request("req-1", notify=False)

        self.assertEqual("req-1", result["req_id"])
        self.assertEqual("retrying", result["status"])
        spawn.assert_called_once_with(candidate, "req-1", False)
        update.assert_called_once_with(
            "req-1", status="requested", error=None, progress=None,
            concept_sheet_path=None, concepts=None, precision_profile=None,
            quality_report=None,
            retry_count=2,
        )

    def test_nonfailed_request_cannot_spawn_duplicate_worker(self):
        with patch.object(babelforge_mcp.catalog, "get", return_value={
                "id": "req-1", "status": "translating", "selected": {},
        }), patch.object(babelforge_mcp, "_spawn_pipeline") as spawn:
            result = babelforge_mcp.retry_request("req-1")

        self.assertIn("only failed", result["error"])
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
