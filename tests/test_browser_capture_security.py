import json
import tempfile
import unittest
from pathlib import Path

try:
    from engine.http_api import required_scope
except ImportError:
    required_scope = None
from engine.service import EngineService


class BrowserCaptureSecurityTests(unittest.TestCase):
    def batch(self, **overrides):
        value = {
            "version": "browser-capture/1",
            "type": "candidate_batch",
            "batch_id": "secure-batch",
            "request_id": "secure-request",
            "origin": {"extension_origin": "chrome-extension://fixture", "page_origin": "https://example.test"},
            "page": {"url": "https://example.test/page"},
            "session_ref": "secure-session",
            "candidates": [{
                "url": "https://cdn.example.test/file.bin?signature=top-secret&token=browser-token",
                "headers": {"Authorization": "Bearer browser-secret", "Cookie": "session-cookie",
                            "Referer": "https://example.test/page"},
                "filename": "file.bin",
            }],
        }
        for key, item in overrides.items():
            if key == "origin":
                value["origin"].update(item)
            else:
                value[key] = item
        return value

    def service(self):
        raw = tempfile.TemporaryDirectory()
        service = EngineService(raw.name)
        service.dispatch("session_register", {"reference": "secure-session", "provider_id": "generic",
                                               "expires_at": 4102444800})
        return raw, service

    def test_rejects_unadmitted_origins_and_expired_sessions(self):
        raw, service = self.service()
        try:
            with self.assertRaises(ValueError):
                service.dispatch("browser_capture_batch", self.batch(origin={"extension_origin": "https://evil.test"}))
            service.dispatch("session_refresh", {"reference": "secure-session", "expires_at": 1, "state": "active"})
            with self.assertRaises(ValueError):
                service.dispatch("browser_capture_batch", self.batch(batch_id="expired", request_id="expired-request"))
        finally:
            service.close()
            raw.cleanup()

    def test_rejects_unsafe_scheme_oversize_batches_and_malformed_headers(self):
        raw, service = self.service()
        try:
            with self.assertRaises(ValueError):
                service.dispatch("browser_capture_batch", self.batch(candidates=[{"url": "file:///secret"}]))
            with self.assertRaises(ValueError):
                service.dispatch("browser_capture_batch", self.batch(batch_id="oversized", request_id="oversized-request",
                                                                       candidates=[{"url": "https://example.test/a"}] * 257))
            with self.assertRaises(ValueError):
                service.dispatch("browser_capture_batch", self.batch(batch_id="headers", request_id="headers-request",
                                                                       candidates=[{"url": "https://example.test/a", "headers": ["Authorization"]}]))
        finally:
            service.close()
            raw.cleanup()

    def test_secret_material_is_absent_from_batch_storage_events_and_imported_task(self):
        raw, service = self.service()
        try:
            result = service.dispatch("browser_capture_batch", self.batch())
            serialized = json.dumps(result) + json.dumps(service.store.list_capture_batches())
            serialized += json.dumps(service.events.pending())
            self.assertNotIn("top-secret", serialized)
            self.assertNotIn("browser-token", serialized)
            self.assertNotIn("browser-secret", serialized)
            self.assertNotIn("session-cookie", serialized)
            imported = service.dispatch("capture_import", {"batch_id": "secure-batch", "candidate_index": 0})
            task = service.store.get(imported["task"]["id"])
            self.assertNotIn("browser-secret", json.dumps(task.to_dict()))
            self.assertNotIn("top-secret", json.dumps(task.to_dict()))
        finally:
            service.close()
            raw.cleanup()

    def test_browser_scope_map_is_explicit_and_does_not_grant_generic_dispatch(self):
        self.assertIsNotNone(required_scope)
        self.assertEqual(required_scope("capture_review_list"), "read")
        self.assertEqual(required_scope("capture_review_get"), "read")
        self.assertEqual(required_scope("capture_import"), "enqueue")
        self.assertIsNone(required_scope("browser_unknown_method"))


if __name__ == "__main__":
    unittest.main()
