from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import hashlib
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine.custom_downloader import CustomAsyncBackend
from engine.concurrency_auditor import concurrency_auditor
from engine.limits import AdaptiveCircuitBreaker, AdaptiveHostWindow, CircuitOpenError, ResourceManager, SchedulerPolicy
from engine.models import ResolvedItem
from engine.rust_backend import RustTransferBackend
from engine.scheduler import FairAsyncScheduler


class _RangeHandler(BaseHTTPRequestHandler):
    body = bytes((index % 251 for index in range(5 * 1024 * 1024)))
    supports_ranges = True
    fail_ranges = False
    range_active = 0
    range_peak = 0
    range_lock = threading.Lock()
    overlap_barrier: threading.Barrier | None = None
    status_counts: dict[int, int] = {}

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("ETag", '"fixture-v1"')
        if self.supports_ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        value = self.headers.get("Range", "")
        start, end = 0, len(self.body) - 1
        status = 200
        if value.startswith("bytes=") and self.supports_ranges and not self.fail_ranges:
            start_text, _, end_text = value[6:].partition("-")
            start = int(start_text)
            end = int(end_text) if end_text else end
            status = 206
        payload = self.body[start : end + 1]
        ranged = status == 206
        with type(self).range_lock:
            type(self).status_counts[status] = type(self).status_counts.get(status, 0) + 1
            if ranged:
                type(self).range_active += 1
                type(self).range_peak = max(type(self).range_peak, type(self).range_active)
        try:
            barrier = type(self).overlap_barrier
            if ranged and barrier is not None:
                try:
                    barrier.wait(timeout=10.0)
                except threading.BrokenBarrierError:
                    pass
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("ETag", '"fixture-v1"')
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.body)}")
            self.end_headers()
            if ranged:
                time.sleep(0.02)
            self.wfile.write(payload)
        finally:
            if ranged:
                with type(self).range_lock:
                    type(self).range_active -= 1

    def log_message(self, *_args):
        return


class CustomEngineTests(unittest.TestCase):
    artifacts = Path(__file__).parent / ".test-artifacts" / "custom"

    def setUp(self):
        # The auditor is a process-wide singleton: an earlier test's confirmed
        # pressure on loopback would freeze this host cap and serialize the
        # segment window. Start every case from a clean breaker for the fixture host.
        concurrency_auditor.reset_breaker("127.0.0.1")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _item(self) -> ResolvedItem:
        url = f"http://127.0.0.1:{self.server.server_port}/fixture.bin"
        return ResolvedItem("generic", url, "fixture.bin", size=len(_RangeHandler.body), direct_url=url)

    def test_custom_engine_downloads_balanced_ranges(self):
        target = self.artifacts / "segmented"
        target.mkdir(parents=True, exist_ok=True)
        destination = target / "fixture.bin"
        destination.unlink(missing_ok=True)
        resources = ResourceManager(SchedulerPolicy(max_active_tasks=2, max_active_segments=4, per_host_transfers=2, max_segments_per_file=4, min_segment_size=1024 * 1024))
        backend = CustomAsyncBackend(resources)
        asyncio.run(backend.download(self._item(), target))
        self.assertEqual(destination.read_bytes(), _RangeHandler.body)
        self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), hashlib.sha256(_RangeHandler.body).hexdigest())
        self.assertFalse(destination.with_name(destination.name + ".part").exists())

    def test_custom_engine_admits_overlapping_range_requests(self):
        target = self.artifacts / "overlap"
        target.mkdir(parents=True, exist_ok=True)
        destination = target / "fixture.bin"
        destination.unlink(missing_ok=True)
        with _RangeHandler.range_lock:
            _RangeHandler.range_active = 0
            _RangeHandler.range_peak = 0
            _RangeHandler.status_counts = {}
        _RangeHandler.overlap_barrier = threading.Barrier(2, timeout=10.0)
        self.addCleanup(setattr, _RangeHandler, "overlap_barrier", None)
        resources = ResourceManager(SchedulerPolicy(
            max_active_segments=4,
            per_host_transfers=4,
            max_segments_per_file=4,
            initial_segment_concurrency=4,
            min_segment_size=1024 * 1024,
        ))
        asyncio.run(CustomAsyncBackend(resources).download(self._item(), target))
        self.assertEqual(destination.read_bytes(), _RangeHandler.body)
        self.assertGreaterEqual(
            _RangeHandler.range_peak, 2,
            f"range requests did not overlap; status counts: {_RangeHandler.status_counts}")

    def test_custom_engine_falls_back_when_ranges_are_unavailable(self):
        _RangeHandler.supports_ranges = False
        try:
            target = self.artifacts / "single"
            target.mkdir(parents=True, exist_ok=True)
            destination = target / "fixture.bin"
            destination.unlink(missing_ok=True)
            resources = ResourceManager(SchedulerPolicy(min_segment_size=1024))
            asyncio.run(CustomAsyncBackend(resources).download(self._item(), target))
            self.assertEqual(destination.read_bytes(), _RangeHandler.body)
        finally:
            _RangeHandler.supports_ranges = True

    def test_custom_engine_discards_preallocated_layout_on_range_failure(self):
        _RangeHandler.fail_ranges = True
        try:
            target = self.artifacts / "range-failure"
            target.mkdir(parents=True, exist_ok=True)
            destination = target / "fixture.bin"
            destination.unlink(missing_ok=True)
            resources = ResourceManager(SchedulerPolicy(min_segment_size=1024, max_segments_per_file=4))
            asyncio.run(CustomAsyncBackend(resources).download(self._item(), target))
            self.assertEqual(destination.read_bytes(), _RangeHandler.body)
        finally:
            _RangeHandler.fail_ranges = False

    def test_adaptive_window_reduces_then_recovers(self):
        async def exercise():
            window = AdaptiveHostWindow(4)
            await window.acquire()
            await window.release(False)
            self.assertEqual(window.window, 1)
            await window.acquire()
            await window.release(True)
            await window.acquire()
            await window.release(True)
            self.assertEqual(window.window, 2)

        asyncio.run(exercise())

    def test_circuit_breaker_opens_after_repeated_failures(self):
        async def exercise():
            breaker = AdaptiveCircuitBreaker(threshold=2, cooldown=0.01)
            await breaker.record(False)
            await breaker.record(False)
            with self.assertRaises(CircuitOpenError):
                await breaker.before()
            await asyncio.sleep(1.05)
            await breaker.before()

        asyncio.run(exercise())

    def test_scheduler_rotates_between_fairness_groups(self):
        async def exercise():
            scheduler = FairAsyncScheduler(1)
            await scheduler.start()
            observed = []

            async def job(label):
                observed.append(label)

            await asyncio.gather(
                scheduler.submit("provider-a", lambda: job("a1")),
                scheduler.submit("provider-a", lambda: job("a2")),
                scheduler.submit("provider-b", lambda: job("b1")),
            )
            await scheduler.close()
            self.assertEqual(observed, ["a1", "b1", "a2"])

        asyncio.run(exercise())

    def test_provider_and_account_slots_bound_concurrency(self):
        async def exercise():
            resources = ResourceManager(SchedulerPolicy(per_provider_transfers=1, per_account_transfers=1))
            item = ResolvedItem("gofile", "source", "file", metadata={"account_ref": "acct"})
            active = 0
            peak = 0
            lock = asyncio.Lock()

            async def job():
                nonlocal active, peak
                async with resources.item_slot(item):
                    async with lock:
                        active += 1
                        peak = max(peak, active)
                    await asyncio.sleep(0.01)
                    async with lock:
                        active -= 1

            await asyncio.gather(job(), job())
            self.assertEqual(peak, 1)

        asyncio.run(exercise())

    def test_rust_transfer_core_round_trip_when_built(self):
        backend = RustTransferBackend(min_segment_size=1024 * 1024)
        if not backend.available():
            self.skipTest("transfer-core binary has not been built")
        target = self.artifacts / "rust"
        target.mkdir(parents=True, exist_ok=True)
        destination = target / "fixture.bin"
        destination.unlink(missing_ok=True)
        path = backend.download(self._item(), target)
        self.assertEqual(path, destination)
        self.assertEqual(destination.read_bytes(), _RangeHandler.body)

    def test_rust_transfer_core_reuses_completed_segment_parts(self):
        backend = RustTransferBackend(min_segment_size=1024 * 1024)
        if not backend.available():
            self.skipTest("transfer-core binary has not been built")
        target = self.artifacts / "rust-resume"
        target.mkdir(parents=True, exist_ok=True)
        destination = target / "fixture.bin"
        destination.unlink(missing_ok=True)
        parts = destination.with_suffix(".transfer-parts")
        parts.mkdir(parents=True, exist_ok=True)
        segment_size = len(_RangeHandler.body) // 4
        (parts / "0.part").write_bytes(_RangeHandler.body[:segment_size])
        path = backend.download(self._item(), target)
        self.assertEqual(path.read_bytes(), _RangeHandler.body)
        self.assertFalse(parts.exists())


if __name__ == "__main__":
    unittest.main()
