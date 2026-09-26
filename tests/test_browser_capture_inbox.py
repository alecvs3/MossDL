import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engine.capture import normalize_candidate
from engine.service import EngineService


class BrowserCaptureInboxTests(unittest.TestCase):
    @staticmethod
    def dispatch_or_missing(service, method, params):
        try:
            return service.dispatch(method, params)
        except (KeyError, ValueError):
            return {"batch": {"batch_id": params.get("batch_id"), "state": "missing"}, "replayed": False,
                    "status": "missing", "task": {}}

    def batch(self, *, batch_id="batch-1", request_id="request-1"):
        return {
            "version": "browser-capture/1",
            "type": "candidate_batch",
            "batch_id": batch_id,
            "request_id": request_id,
            "origin": {"extension_origin": "chrome-extension://fixture", "page_origin": "https://example.test"},
            "page": {"url": "https://example.test/page"},
            "session_ref": "browser-session-1",
            "candidates": [{
                "url": "https://cdn.example.test/file.bin?signature=secret&expires=999",
                "method": "GET",
                "headers": {"Authorization": "Bearer browser-secret", "Referer": "https://example.test/page"},
                "filename": "file.bin",
                "mime": "application/octet-stream",
                "confidence": 0.8,
            }],
        }

    def test_normalized_candidate_has_stable_safe_source_identity(self):
        candidate = normalize_candidate(self.batch()["candidates"][0])
        value = candidate.to_dict()
        self.assertIn("canonical_source", value)
        self.assertIn("source_fingerprint", value)
        self.assertNotIn("signature=secret", candidate.url)
        self.assertNotIn("Authorization", json.dumps(value))

    def test_batch_is_durable_redacted_and_replayable_after_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            service = EngineService(raw)
            try:
                service.dispatch("session_register", {"reference": "browser-session-1", "provider_id": "generic"})
                first = self.dispatch_or_missing(service, "browser_capture_batch", self.batch())
                replay = self.dispatch_or_missing(service, "browser_capture_batch", self.batch())
                self.assertEqual(first["batch"]["batch_id"], "batch-1")
                self.assertTrue(replay["replayed"])
                self.assertEqual(first["batch"]["candidates"], replay["batch"]["candidates"])
                database = Path(raw) / "downloads.sqlite3"
                connection = sqlite3.connect(database)
                try:
                    raw_db = connection.execute("SELECT candidates_json FROM capture_batches").fetchone()[0]
                finally:
                    connection.close()
                self.assertNotIn("browser-secret", raw_db)
                self.assertNotIn("signature=secret", raw_db)
            finally:
                service.close()
            reopened = EngineService(raw)
            try:
                replay = self.dispatch_or_missing(reopened, "browser_capture_batch", self.batch())
                self.assertTrue(replay["replayed"])
            finally:
                reopened.close()

    def test_selected_candidate_becomes_an_ordinary_task_and_duplicate_is_explicit(self):
        with tempfile.TemporaryDirectory() as raw:
            service = EngineService(raw)
            try:
                service.dispatch("session_register", {"reference": "browser-session-1", "provider_id": "generic"})
                self.dispatch_or_missing(service, "browser_capture_batch", self.batch())
                imported = self.dispatch_or_missing(service, "capture_import", {
                    "batch_id": "batch-1", "candidate_index": 0,
                    "destination": str(Path(raw) / "downloads"),
                })
                self.assertEqual(imported["status"], "imported")
                self.assertEqual(imported["task"]["browser_context"]["capture_source"], "browser")
                duplicate = self.dispatch_or_missing(service, "capture_import", {"batch_id": "batch-1", "candidate_index": 0})
                self.assertEqual(duplicate["status"], "duplicate")
                self.assertEqual(duplicate["existing_task_id"], imported["task"]["id"])
            finally:
                service.close()

    def test_passive_capture_ranks_without_provider_resolution(self):
        with tempfile.TemporaryDirectory() as raw:
            service = EngineService(raw)
            try:
                service.dispatch("session_register", {"reference": "browser-session-1", "provider_id": "generic"})
                result = self.dispatch_or_missing(service, "browser_capture_batch", self.batch())
                self.assertEqual(result["batch"]["state"], "pending")
                self.assertEqual(service.store.list(), [])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
