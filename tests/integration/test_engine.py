from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import shutil
import threading
import time
from tempfile import TemporaryDirectory
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from engine.db import TaskStore
from engine.custom_downloader import CustomAsyncBackend, _replace_verified
from engine.limits import ResourceManager, SchedulerPolicy, normalize_bandwidth_rate
from engine.models import ArchiveJob, DownloadTask, ResolvedItem, TransferLease
from engine.plugins import PluginProcess, PluginRegistry
from engine.providers.generic import GenericProvider
from engine.service import EngineService
from engine.backend_selection import BackendSelector
from engine.transfer_contracts import BackendCapabilities
from engine.transport_metrics import TransportMetricsSink
from engine.segment_stealer import DynamicSegmentCoordinator


class _Handler(BaseHTTPRequestHandler):
    body = b"transfer-manager-test" * 100

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("Content-Disposition", 'attachment; filename="fixture.bin"')
        self.end_headers()

    def do_GET(self):  # noqa: N802
        start = 0
        value = self.headers.get("Range", "")
        if value.startswith("bytes="):
            start = int(value[6:].split("-")[0])
        payload = self.body[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Range", f"bytes {start}-{len(self.body)-1}/{len(self.body)}")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


class EngineTests(unittest.TestCase):
    artifacts = Path(__file__).parent / ".test-artifacts"

    @classmethod
    def setUpClass(cls):
        cls.artifacts.mkdir(exist_ok=True)

    def test_isolated_plugin_process_contract(self):
        process = PluginProcess("generic")
        try:
            self.assertEqual(process.call("manifest")["id"], "generic")
            self.assertTrue(process.call("match", {"url": "https://example.test/file.bin"}))
        finally:
            process.close()

    def test_registry_selects_provider(self):
        registry = PluginRegistry()
        try:
            self.assertEqual(registry.provider_for("https://gofile.io/d/abc"), "gofile")
            self.assertEqual(registry.provider_for("https://transfer.it/t/b5y3bbj7wiqk"), "transfer.it")
            self.assertEqual(registry.provider_for("ftp://example.test/file.zip"), "generic")
        finally:
            registry.close()

    def test_sqlite_round_trip(self):
        directory = self.artifacts / "database"
        directory.mkdir(exist_ok=True)
        store = TaskStore(directory / "tasks.sqlite3")
        task = DownloadTask("https://example.test/file", str(directory))
        task.resolved = [ResolvedItem("generic", task.source_url, "file", direct_url=task.source_url)]
        store.save(task)
        loaded = store.get(task.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.resolved[0].display_name, "file")
        store.close()

    def test_durable_plan_leases_archive_jobs_and_outbox(self):
        directory = self.artifacts / "durable"
        directory.mkdir(exist_ok=True)
        store = TaskStore(directory / "tasks.sqlite3")
        task = DownloadTask("https://example.test/file", str(directory))
        store.save_with_event(task, "TaskCreated", task.to_dict(), f"created:{task.id}")
        store.save_plan(task.id, "generic", str(directory), 12, {"max_file_size": 99}, None, {"items": []})
        store.save_segment(task.id, 0, 0, 0, 11, True, "completed")
        lease = TransferLease("lease-1", task.id, "example.test", "generic", expires_at=time.time() + 60)
        store.create_lease(lease)
        self.assertEqual(len(store.list_leases(active_only=True)), 1)
        store.release_lease("lease-1")
        store.save_archive_job(ArchiveJob("job-1", task.id, "in.zip", "out"))
        self.assertEqual(store.get_archive_job("job-1").state, "queued")
        events = store.pending_events()
        self.assertEqual(events[0]["event_type"], "TaskCreated")
        store.mark_event_published(events[0]["id"])
        self.assertEqual(store.pending_events(), [])
        store.close()

    def test_generic_resolve_uses_content_disposition(self):
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/download"
            item = GenericProvider.resolve(url)[0]
            self.assertEqual(item.display_name, "fixture.bin")
            self.assertEqual(item.size, len(_Handler.body))
            self.assertEqual(item.headers["User-Agent"], "transfer-manager/0.1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_service_uses_shared_async_scheduler(self):
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        service = EngineService(self.artifacts / "service")
        try:
            url = f"http://127.0.0.1:{server.server_port}/download"
            task = service.dispatch("add_task", {"url": url, "destination": str(self.artifacts / "service-download")})
            service.dispatch("download_task", {"id": task["id"], "backend": "custom"})
            deadline = time.monotonic() + 8
            loaded = None
            while time.monotonic() < deadline:
                loaded = service.store.get(task["id"])
                if loaded and loaded.state in {"completed", "failed", "canceled"}:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.state, "completed")
            self.assertEqual(loaded.completed_bytes, len(_Handler.body))
        finally:
            service.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_tracer_auto_selection_emits_redacted_metrics(self):
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        target_dir = self.artifacts / "tracer-selection"
        dest_dir = self.artifacts / "tracer-selection-download"
        shutil.rmtree(target_dir, ignore_errors=True)
        shutil.rmtree(dest_dir, ignore_errors=True)
        service = EngineService(target_dir)
        try:
            event_cursor = service.store.latest_event_id()
            url = f"http://127.0.0.1:{server.server_port}/download?signature=fixture-secret"
            task = service.dispatch("add_task", {
                "url": url,
                "destination": str(dest_dir),
            })
            service.dispatch("download_task", {"id": task["id"]})
            deadline = time.monotonic() + 15
            loaded = None
            while time.monotonic() < deadline:
                loaded = service.store.get(task["id"])
                if loaded and loaded.state in {"completed", "failed", "canceled"}:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.state, "completed")
            # TaskTransportMetrics is emitted asynchronously around completion;
            # wait (bounded) for it instead of racing the writer under load.
            events: list[dict[str, Any]] = []
            metrics: list[dict[str, Any]] = []
            metric_deadline = time.monotonic() + 5
            while True:
                events = service.store.events_since(event_cursor, 500)
                metrics = [event for event in events if event["event_type"] == "TaskTransportMetrics" and event["task_id"] == task["id"]]
                if metrics or time.monotonic() >= metric_deadline:
                    break
                time.sleep(0.05)
            selected = [event for event in events if event["event_type"] == "TaskBackendSelected" and event["task_id"] == task["id"]]
            self.assertEqual(len(selected), 1)
            self.assertTrue(metrics, "TaskTransportMetrics event was not emitted within the wait window")
            selected_payload = json.loads(selected[0]["payload_json"])
            self.assertIn(selected_payload["backend"], {"custom", "rust"})
            self.assertEqual(loaded.backend, selected_payload["backend"])
            metric_payload = json.loads(metrics[-1]["payload_json"])
            self.assertEqual(metric_payload["selected_backend"], selected_payload["backend"])
            # The fixture server publishes only Content-Length, so the length is
            # all we ever checked. `size_verified` says exactly that; reporting
            # "verified" would claim content proof this download never had.
            self.assertEqual(metric_payload["integrity_outcome"], "size_verified")
            self.assertGreaterEqual(metric_payload["bytes_written"], len(_Handler.body))
            serialized = json.dumps({"selected": selected, "metrics": metrics}).lower()
            for forbidden in ("signature=", "cookie", "authorization", "signed_url", "password", "token"):
                self.assertNotIn(forbidden, serialized)
        finally:
            service.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_completed_task_cannot_be_restarted_or_resumed(self):
        service = EngineService(self.artifacts / "terminal-state")
        try:
            task = service.dispatch("add_task", {
                "url": "https://example.test/completed.bin",
                "destination": str(self.artifacts / "terminal-state-download"),
            })
            stored = service.store.get(task["id"])
            self.assertIsNotNone(stored)
            stored.state = "completed"
            service.store.save(stored)

            # download_task returns completed task idempotently to prevent supervisor crashes
            res = service.dispatch("download_task", {"id": task["id"]})
            self.assertEqual(res["state"], "completed")
            with self.assertRaisesRegex(ValueError, "completed downloads cannot be started or resumed"):
                service.dispatch("resume_task", {"id": task["id"]})
            with self.assertRaisesRegex(ValueError, "terminal download state completed cannot transition to paused"):
                service.dispatch("pause_task", {"id": task["id"]})
            with self.assertRaisesRegex(ValueError, "terminal download state completed cannot be changed"):
                service.dispatch("task_recover", {"id": task["id"]})

            self.assertEqual(service.store.get(task["id"]).state, "completed")
        finally:
            service.close()

    def test_cancel_stops_active_task(self):
        service = EngineService(self.artifacts / "cancel-active")
        try:
            task = service.dispatch("add_task", {
                "url": "https://example.test/slow.bin",
                "destination": str(self.artifacts / "cancel-active-download"),
            })
            active = service.store.get(task["id"])
            self.assertIsNotNone(active)
            active.state = "downloading"
            service.store.save(active)
            from engine.service import _TaskControl
            control = _TaskControl()
            service._controls[active.id] = control
            result = service.dispatch("cancel_task", {"id": active.id})
            self.assertEqual(result["state"], "canceled")
            self.assertTrue(control.cancel.is_set())
            self.assertEqual(service.store.get(active.id).state, "canceled")
        finally:
            service.close()

    def test_retry_exhaustion_is_durable(self):
        directory = self.artifacts / "retry-exhaustion"
        directory.mkdir(exist_ok=True)
        database = directory / "downloads.sqlite3"
        store = TaskStore(database)
        task = DownloadTask("https://example.test/retry", str(directory), state="failed",
                            retry_count=3, error="retry budget exhausted")
        store.save(task)
        store.close()
        reopened = TaskStore(database)
        try:
            loaded = reopened.get(task.id)
            self.assertEqual(loaded.state, "failed")
            self.assertEqual(loaded.retry_count, 3)
            self.assertEqual(loaded.error, "retry budget exhausted")
        finally:
            reopened.close()

    def test_checksum_failure_rejects_completion(self):
        directory = self.artifacts / "checksum-failure"
        directory.mkdir(exist_ok=True)
        path = directory / "file.bin"
        path.write_bytes(b"actual")
        item = ResolvedItem("generic", "source", "file.bin", size=6, direct_url="https://example.test/file",
                            checksum="sha256:" + ("0" * 64))
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            CustomAsyncBackend._verify(path, item, expected_size=6)

    def test_verified_replacement_is_atomic(self):
        directory = self.artifacts / "atomic-replacement"
        directory.mkdir(exist_ok=True)
        source, destination = directory / "verified.part", directory / "file.bin"
        source.write_bytes(b"new verified content")
        destination.write_bytes(b"old content")
        _replace_verified(source, destination)
        self.assertEqual(destination.read_bytes(), b"new verified content")
        self.assertFalse(source.exists())

    def test_restart_recovers_manifest(self):
        with TemporaryDirectory(dir=self.artifacts) as raw:
            manifest = Path(raw) / "file.part.segments.json"
            coordinator = DynamicSegmentCoordinator(manifest, 12_000, min_steal_bytes=1_000)
            coordinator.load_or_init([(0, 5_999), (6_000, 11_999)])
            first = coordinator.claim_work("worker")
            coordinator.record_progress(first.id, 2_000)
            recovered = DynamicSegmentCoordinator(manifest, 12_000, min_steal_bytes=1_000)
            recovered.load_or_init([(0, 5_999), (6_000, 11_999)])
            self.assertEqual(recovered.segments[0].done, 2_000)
            self.assertEqual(recovered.segments[0].status, "pending")
            self.assertEqual(sum(segment.expected for segment in recovered.segments), 12_000)

    def test_completed_task_cannot_reactivate(self):
        service = EngineService(self.artifacts / "terminal-recheck")
        try:
            task = service.dispatch("add_task", {
                "url": "https://example.test/completed.bin",
                "destination": str(self.artifacts / "terminal-recheck-download"),
            })
            stored = service.store.get(task["id"])
            stored.state = "completed"
            service.store.save(stored)
            res = service.dispatch("download_task", {"id": task["id"]})
            self.assertEqual(res["state"], "completed")
            with self.assertRaises(ValueError):
                service.dispatch("task_recover", {"id": task["id"]})
            self.assertEqual(service.store.get(task["id"]).state, "completed")
        finally:
            service.close()

    def test_bandwidth_profiles_are_bytes_per_second_with_scope_diagnostics(self):
        self.assertEqual(normalize_bandwidth_rate(1, unit="mebibytes_per_second"), 1024 * 1024)
        self.assertEqual(normalize_bandwidth_rate(0), 0)
        with self.assertRaises(ValueError):
            normalize_bandwidth_rate(True)
        resources = ResourceManager(SchedulerPolicy(bandwidth_bytes_per_second=0))
        self.assertEqual(resources.bandwidth_diagnostics(), {
            "rate_bytes_per_second": 0, "scope": "global", "unlimited": True,
            "unit": "bytes_per_second",
        })
        service = EngineService(self.artifacts / "bandwidth-scopes")
        try:
            for scope, key in (("global", None), ("queue", "default"), ("provider", "generic"),
                               ("account", "acct"), ("task", "task")):
                service.store.save_bandwidth_profile({
                    "id": f"{scope}-profile", "name": scope, "scope": scope, "scope_key": key,
                    "rate_bytes_per_second": 4096,
                })
            task = service.dispatch("add_task", {
                "url": "https://example.test/file", "destination": str(self.artifacts / "bandwidth-file"),
            })
            stored = service.store.get(task["id"])
            service.store.save_bandwidth_profile({
                "id": "task-profile", "name": "task", "scope": "task", "scope_key": stored.id,
                "rate_bytes_per_second": 12345,
            })
            diagnostic = service.dispatch("get_effective_bandwidth", {"task_id": stored.id})
            self.assertEqual(diagnostic["rate_bytes_per_second"], 12345)
            self.assertEqual(diagnostic["scope"], "task")
            self.assertEqual(diagnostic["unit"], "bytes_per_second")
        finally:
            service.close()

    def test_backend_selector_ranks_available_capability_matches(self):
        class Adapter:
            def __init__(self, capabilities):
                self._capabilities = capabilities

            def capabilities(self):
                return self._capabilities

            def available(self):
                return self._capabilities.available

        item = ResolvedItem("generic", "source", "file", size=10, direct_url="https://example.test/file",
                            metadata={"requires_ranges": True})
        backends = {
            "custom": Adapter(BackendCapabilities("custom", supports_ranges=True)),
            "rust": Adapter(BackendCapabilities("rust", supports_ranges=True, available=False,
                                                availability_reason="fixture binary absent")),
        }
        decision = BackendSelector().select(item, {"kind": "direct"}, backends)
        self.assertTrue(decision.compatible)
        self.assertEqual(decision.backend, "custom")
        self.assertIn("rust", " ".join(decision.diagnostics))

    def test_backend_selector_rejects_unsafe_override(self):
        class Adapter:
            def capabilities(self):
                return BackendCapabilities("custom", supports_media_segments=False)

            def available(self):
                return True

        item = ResolvedItem("media", "source", "video.mp4", metadata={"media_plan": {}})
        decision = BackendSelector().select(item, {"kind": "direct"}, {"custom": Adapter()}, "custom")
        self.assertFalse(decision.compatible)
        self.assertEqual(decision.backend, "custom")
        self.assertIn("media segments", decision.reason)

    def test_transfer_canary_policy_is_persisted_and_operation_scoped_by_default(self):
        with TemporaryDirectory(dir=self.artifacts) as raw:
            service = EngineService(raw)
            try:
                initial = service.dispatch("backend_canary_status")
                self.assertFalse(initial["enabled"])
                self.assertEqual(initial["scope"], "operation")
                updated = service.dispatch("backend_canary_update", {"policy": {
                    "enabled": True,
                    "fallback_backend": "custom",
                    "failure_classes": ["unsupported"],
                }})
                self.assertTrue(updated["enabled"])
                self.assertEqual(updated["fallback_backend"], "custom")
                self.assertIsNone(updated["disabled_until"])

                state = service._record_canary_failure("operation-1", "rust", "unsupported", "unsupported fixture")
                self.assertIsNotNone(state)
                self.assertEqual(service._canary_backend_override("operation-1", None)[0], "custom")
                self.assertEqual(service._canary_policy()["disabled_until"], None)
                self.assertEqual(service._canary_policy()["reason"], None)

                service._clear_canary_operation("operation-1")
                self.assertIsNone(service._canary_backend_override("operation-1", None)[0])
                persisted = service.store.get_setting("transfer_canary_policy")
                self.assertEqual(persisted["scope"], "operation")
                self.assertEqual(persisted["fallback_backend"], "custom")
            finally:
                service.close()

    def test_transfer_canary_requires_explicit_broader_scope_and_survives_restart(self):
        with TemporaryDirectory(dir=self.artifacts) as raw:
            directory = Path(raw)
            service = EngineService(directory)
            try:
                with self.assertRaisesRegex(ValueError, "authorize_broader_scope"):
                    service.dispatch("backend_canary_update", {"policy": {"enabled": True, "scope": "global"}})
                service.dispatch("backend_canary_update", {"policy": {
                    "enabled": True, "scope": "global", "authorize_broader_scope": True,
                    "failure_classes": ["transient_network"],
                }})
                service._record_canary_failure("operation-2", "rust", "transient_network", "fixture network failure")
                status = service.dispatch("backend_canary_status")
                self.assertTrue(status["disabled_until"] > 0)
                self.assertEqual(service._canary_backend_override("operation-3", None)[0], "custom")
            finally:
                service.close()
            reopened = EngineService(directory)
            try:
                status = reopened.dispatch("backend_canary_status")
                self.assertEqual(status["scope"], "global")
                self.assertTrue(status["disabled_until"] > 0)
                self.assertEqual(reopened._canary_backend_override("operation-4", None)[0], "custom")
                reset = reopened.dispatch("backend_canary_reset")
                self.assertIsNone(reset["disabled_until"])
            finally:
                reopened.close()

    def test_transport_metrics_redact_operation_material(self):
        sink = TransportMetricsSink()
        metrics = sink.begin("task-1", "custom", reason="refresh https://example.test/file?token=secret")
        metrics.start(now=10.0)
        metrics.record_connection()
        metrics.record_read(128, 0.25)
        metrics.record_write(128, 0.15)
        metrics.record_retry()
        metrics.record_refresh()
        metrics.record_throttle(0.5)
        metrics.set_integrity("verified")
        snapshot = metrics.snapshot(now=11.0)
        serialized = json.dumps(snapshot).lower()
        self.assertEqual(snapshot["bytes_written"], 128)
        self.assertEqual(snapshot["retry_count"], 1)
        self.assertEqual(snapshot["integrity_outcome"], "verified")
        for forbidden in ("https://", "cookie", "authorization", "signed_url", "password", "token", "secret"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
