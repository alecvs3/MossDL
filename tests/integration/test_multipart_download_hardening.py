from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.archive_backend import completion_sentinel_path
from engine.models import DownloadTask
from engine.rust_backend import find_transfer_core
from engine.service import EngineService
from engine.telemetry import LogEvent, telemetry_bus


class _MockArchiveWorkerBackend:
    """Mock archive backend that creates a completion sentinel so archive jobs succeed."""

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    def extract(self, path: str | Path, output_directory: str | Path, password_ref: str | None = None,
                policy: dict[str, Any] | None = None, control: Any = None, job_id: str | None = None) -> dict[str, Any]:
        self.calls += 1
        out = Path(output_directory)
        out.mkdir(parents=True, exist_ok=True)
        if job_id:
            completion_sentinel_path(out, job_id).write_bytes(b"")
        return {"output_directory": str(out), "files": 1, "bytes": 1024}


@unittest.skipUnless(find_transfer_core(), "Rust transfer-core binary is unavailable")
class TestMultipartDownloadHardening(unittest.TestCase):
    """Integration test suite verifying multi-part package downloading via service and scheduler."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.temp_dir.name)
        self.server_dir = self.root_dir / "server_files"
        self.server_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir = self.root_dir / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.engine_dir = self.root_dir / "engine_data"
        self.engine_dir.mkdir(parents=True, exist_ok=True)

        server_root = str(self.server_dir)

        class _ServerHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, directory=server_root, **kwargs)

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), _ServerHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.server_port = self.server.server_address[1]

        self.dedupe_records: list[LogEvent] = []
        self.fallback_records: list[LogEvent] = []

        def _telemetry_listener(record: LogEvent) -> None:
            msg = getattr(record, "message", "") or ""
            if "[SCHEDULER_DEDUPE]" in msg:
                self.dedupe_records.append(record)
            if "backend_fallback" in msg or "TaskBackendFallback" in msg:
                self.fallback_records.append(record)

        self._telemetry_listener = _telemetry_listener
        telemetry_bus.add_listener(self._telemetry_listener)

        self.service = EngineService(self.engine_dir)
        self.service.archive_backend = _MockArchiveWorkerBackend()

        self.task_updated_events: list[dict[str, Any]] = []
        self._orig_emit = self.service.events.emit

        def _tracking_emit(event_type: str, task_id: str | None, payload: dict[str, Any], dedupe_key: str | None = None) -> int:
            if event_type == "TaskUpdated":
                self.task_updated_events.append({"task_id": task_id, "payload": payload, "dedupe_key": dedupe_key})
            return self._orig_emit(event_type, task_id, payload, dedupe_key=dedupe_key)

        self.service.events.emit = _tracking_emit

    def tearDown(self) -> None:
        telemetry_bus.remove_listener(self._telemetry_listener)
        self.service.close()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2.0)
        self.temp_dir.cleanup()

    def _wait_for_tasks_completion(self, expected_count: int, timeout: float = 20.0) -> list[DownloadTask]:
        """Poll until all expected tasks reach completed, failed, or canceled."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            tasks = self.service.store.list()
            if len(tasks) == expected_count and all(t.state in {"completed", "failed", "canceled"} for t in tasks):
                return tasks
            time.sleep(0.1)
        return self.service.store.list()

    def test_multipart_package_download_rust_backend_no_fallback(self) -> None:
        """Verify multi-part package tasks stay on rust backend, emit TaskUpdated, and finish cleanly."""
        part_contents = {
            "BigData.part1.rar": b"PART1_PAYLOAD_DATA_" * 512,  # ~9.7 KB
            "BigData.part2.rar": b"PART2_PAYLOAD_DATA_" * 512,
            "BigData.part3.rar": b"PART3_PAYLOAD_DATA_" * 512,
        }

        urls = []
        for filename, content in part_contents.items():
            (self.server_dir / filename).write_bytes(content)
            urls.append(f"http://127.0.0.1:{self.server_port}/{filename}")

        t1 = self.service.dispatch("add_task", {"url": urls[0], "destination": str(self.download_dir)})
        t2 = self.service.dispatch("add_task", {"url": urls[1], "destination": str(self.download_dir)})
        t3 = self.service.dispatch("add_task", {"url": urls[2], "destination": str(self.download_dir)})

        # Part 1 is the sentinel leader (queued), Parts 2 and 3 gated in pending_probe
        self.assertEqual(t1["state"], "queued")
        self.assertEqual(t1["package_part_number"], 1)
        self.assertEqual(t2["state"], "pending_probe")
        self.assertEqual(t2["package_part_number"], 2)
        self.assertEqual(t3["state"], "pending_probe")
        self.assertEqual(t3["package_part_number"], 3)

        # Uniform package key stability across all parts
        pkg_key = t1.get("package_key")
        self.assertTrue(pkg_key)
        self.assertEqual(t2.get("package_key"), pkg_key)
        self.assertEqual(t3.get("package_key"), pkg_key)

        tasks = self._wait_for_tasks_completion(3, timeout=20.0)
        self.assertEqual(len(tasks), 3)

        for task in tasks:
            self.assertEqual(task.state, "completed", f"Task {task.display_name} failed: {task.error}")
            # Requirement: Stay on the rust backend across all parts without false fallback
            self.assertEqual(task.backend, "rust", f"Task {task.display_name} backend was {task.backend}, expected rust")

        # Requirement: No false fallback events
        self.assertEqual(len(self.fallback_records), 0, f"Unexpected fallback records: {self.fallback_records}")

        # Requirement: Do not emit duplicate scheduler jobs ([SCHEDULER_DEDUPE])
        self.assertEqual(len(self.dedupe_records), 0, f"Unexpected scheduler dedupe records: {self.dedupe_records}")

        # Requirement: Emit proper progress updates via TaskUpdated
        task_ids = {t.id for t in tasks}
        progress_events = [
            e for e in self.task_updated_events
            if e["task_id"] in task_ids and "completed_bytes" in (e.get("payload") or {})
        ]
        self.assertTrue(progress_events, "No TaskUpdated progress events were captured")

        updated_task_ids = {e["task_id"] for e in progress_events}
        self.assertEqual(updated_task_ids, task_ids, "Every task must emit at least one TaskUpdated progress event")

        for event in progress_events:
            payload = event["payload"]
            self.assertIn("completed_bytes", payload)
            self.assertIn("size", payload)
            self.assertIn("speed_bytes_per_second", payload)
            self.assertIn("average_speed_bytes_per_second", payload)
            self.assertIn("eta_seconds", payload)

        # Requirement: Complete successfully with verified files in a temporary directory
        for filename, expected_data in part_contents.items():
            dest_file = self.download_dir / filename
            self.assertTrue(dest_file.is_file(), f"File {filename} does not exist in destination directory")
            actual_data = dest_file.read_bytes()
            self.assertEqual(len(actual_data), len(expected_data), f"File {filename} size mismatch")
            self.assertEqual(
                hashlib.sha256(actual_data).hexdigest(),
                hashlib.sha256(expected_data).hexdigest(),
                f"File {filename} checksum mismatch",
            )

    def test_multipart_split_binary_chunks_rust_backend(self) -> None:
        """Verify numbered split chunks (.001, .002, .003) stay on rust backend and complete cleanly."""
        part_contents = {
            "archive.bin.001": b"CHUNK_001_DATA_" * 300,
            "archive.bin.002": b"CHUNK_002_DATA_" * 300,
            "archive.bin.003": b"CHUNK_003_DATA_" * 300,
        }

        urls = []
        for filename, content in part_contents.items():
            (self.server_dir / filename).write_bytes(content)
            urls.append(f"http://127.0.0.1:{self.server_port}/{filename}")

        for url in urls:
            self.service.dispatch("add_task", {"url": url, "destination": str(self.download_dir)})

        tasks = self._wait_for_tasks_completion(3, timeout=20.0)
        self.assertEqual(len(tasks), 3)

        for task in tasks:
            self.assertEqual(task.state, "completed", f"Task {task.display_name} failed: {task.error}")
            self.assertEqual(task.backend, "rust", f"Task {task.display_name} did not use rust backend")

        self.assertEqual(len(self.fallback_records), 0)
        self.assertEqual(len(self.dedupe_records), 0)

        for filename, expected_data in part_contents.items():
            dest_file = self.download_dir / filename
            self.assertTrue(dest_file.is_file())
            self.assertEqual(dest_file.read_bytes(), expected_data)

    def test_scheduler_dedupe_sentinel_rejects_duplicate_submissions(self) -> None:
        """Verify scheduler dedupe guard actively emits [SCHEDULER_DEDUPE] when duplicate keys are submitted."""
        scheduler = self.service._scheduler
        self.assertIsNotNone(scheduler, "Scheduler must be initialized")

        job_entered = threading.Event()
        job_release = threading.Event()

        async def _mock_work() -> None:
            job_entered.set()
            while not job_release.is_set():
                await asyncio.sleep(0.02)

        # Submit first job to scheduler
        future1 = asyncio.run_coroutine_threadsafe(
            scheduler.submit("test_group", _mock_work, priority=0, dedupe_key="test:dedupe:key"),
            self.service._loop,
        )

        self.assertTrue(job_entered.wait(timeout=5.0), "First job did not start")

        # Submit duplicate job with identical dedupe_key while first job is in-flight
        dedupe_count_before = len(self.dedupe_records)
        future2 = asyncio.run_coroutine_threadsafe(
            scheduler.submit("test_group", _mock_work, priority=0, dedupe_key="test:dedupe:key"),
            self.service._loop,
        )

        # Duplicate submission must return immediately
        future2.result(timeout=5.0)

        # Verify [SCHEDULER_DEDUPE] was recorded for the duplicate
        self.assertGreater(len(self.dedupe_records), dedupe_count_before)
        last_record = self.dedupe_records[-1]
        msg = getattr(last_record, "message", "")
        ctx = getattr(last_record, "context", {}) or {}
        self.assertIn("[SCHEDULER_DEDUPE]", msg)
        self.assertEqual(ctx.get("dedupe_key"), "test:dedupe:key")

        # Release first job
        job_release.set()
        future1.result(timeout=5.0)

    def test_multipart_concurrent_dispatch_stability(self) -> None:
        """Verify multiple consecutive or concurrent dispatch calls for multipart tasks stay stable."""
        part_contents = {
            "ConcurrentTest.part1.rar": b"CONCURRENT_P1_" * 256,
            "ConcurrentTest.part2.rar": b"CONCURRENT_P2_" * 256,
        }

        urls = []
        for filename, content in part_contents.items():
            (self.server_dir / filename).write_bytes(content)
            urls.append(f"http://127.0.0.1:{self.server_port}/{filename}")

        t1 = self.service.dispatch("add_task", {"url": urls[0], "destination": str(self.download_dir)})
        t2 = self.service.dispatch("add_task", {"url": urls[1], "destination": str(self.download_dir)})

        # Rapidly invoke download_task repeatedly to simulate double-dispatch
        for _ in range(3):
            self.service.dispatch("download_task", {"id": t1["id"]})
            self.service.dispatch("download_task", {"id": t2["id"]})

        tasks = self._wait_for_tasks_completion(2, timeout=20.0)
        self.assertEqual(len(tasks), 2)

        for task in tasks:
            self.assertEqual(task.state, "completed")
            self.assertEqual(task.backend, "rust")

        self.assertEqual(len(self.fallback_records), 0)


if __name__ == "__main__":
    unittest.main()
