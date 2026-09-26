import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from engine.reliability import verify_file
from engine.service import EngineService


class Phase6AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = EngineService(self.temp_dir.name)

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def test_browser_capture_to_enqueue_to_pause_resume_to_integrity_flow(self):
        # 1. Register session
        self.service.dispatch("session_register", {
            "reference": "e2e-session",
            "provider_id": "generic",
            "expires_at": time.time() + 3600,
        })

        # 2. Ingest browser capture batch
        batch = {
            "version": "browser-capture/1",
            "type": "candidate_batch",
            "batch_id": "e2e-batch-01",
            "request_id": "e2e-req-01",
            "origin": {"extension_origin": "chrome-extension://fixture", "page_origin": "https://example.test"},
            "page": {"url": "https://example.test/page"},
            "session_ref": "e2e-session",
            "candidates": [{
                "url": "https://cdn.example.test/archive.bin",
                "headers": {"Referer": "https://example.test/page"},
                "filename": "archive.bin",
            }],
        }
        batch_res = self.service.dispatch("browser_capture_batch", batch)
        self.assertEqual(batch_res["batch"]["batch_id"], "e2e-batch-01")

        # 3. Import candidate into ordinary download task
        import_res = self.service.dispatch("capture_import", {
            "batch_id": "e2e-batch-01",
            "candidate_index": 0,
            "destination": self.temp_dir.name,
        })
        task_id = import_res["task"]["id"]
        self.assertIsNotNone(task_id)

        # 4. Verify TaskCreated event was published to the shared event outbox
        pending = self.service.events.pending()
        self.assertTrue(any(e["event_type"] == "TaskCreated" and e["task_id"] == task_id for e in pending))

        # 5. Pause and Resume task
        pause_res = self.service.dispatch("pause_task", {"id": task_id, "reason": "operator_request"})
        self.assertEqual(pause_res["state"], "paused")

        # Verify TaskPaused event
        pending = self.service.events.pending()
        self.assertTrue(any(e["event_type"] == "TaskPaused" and e["task_id"] == task_id for e in pending))

        resume_res = self.service.dispatch("resume_task", {"id": task_id})
        self.assertEqual(resume_res["state"], "queued")

        # Verify TaskResumed event
        pending = self.service.events.pending()
        self.assertTrue(any(e["event_type"] == "TaskResumed" and e["task_id"] == task_id for e in pending))

        # 6. Simulate file completion & verify integrity
        target_file = Path(self.temp_dir.name) / "archive.bin"
        payload = b"Hello, Phase 6 Deterministic Acceptance Verification!"
        target_file.write_bytes(payload)
        expected_sha256 = hashlib.sha256(payload).hexdigest()

        # Update task to completed with checksum verification
        task_obj = self.service.store.get(task_id)
        task_obj.completed_bytes = len(payload)
        task_obj.size = len(payload)
        task_obj.state = "completed"
        task_obj.integrity_state = "verified"
        task_obj.integrity = {"algorithm": "sha256", "digest": expected_sha256}
        self.service.store.save(task_obj)

        # 7. Check file integrity using engine.reliability.verify_file
        report = verify_file(
            target_file,
            expected_size=len(payload),
            expected_checksum=expected_sha256,
            algorithm="sha256",
        )
        self.assertEqual(report.state, "verified")
        self.assertEqual(report.observed_size, len(payload))

        # 8. Check that headless events_since sees all emitted events
        headless_events = self.service.dispatch("events_since", {"event_id": 0, "limit": 100})
        event_types = {e["event_type"] for e in headless_events}
        self.assertIn("TaskCreated", event_types)
        self.assertIn("TaskPaused", event_types)
        self.assertIn("TaskResumed", event_types)


if __name__ == "__main__":
    unittest.main()