import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import asyncio
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from engine.transport_pool import PooledTransportManager
from engine.segment_stealer import DynamicSegmentCoordinator, SegmentState
from engine.custom_downloader import CustomAsyncBackend
from engine.limits import ResourceManager, SchedulerPolicy
from engine.models import ResolvedItem
from engine.range_planner import RangeCapability, RangePlanner


class HighPerformanceDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory(dir=Path.cwd())
        self.work_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pooled_transport_manager_initialization_and_reuse(self):
        manager = PooledTransportManager(max_keepalive_connections=10, max_connections=20)
        
        # Check default direct client creation
        c1 = manager.get_client()
        c2 = manager.get_client(None)
        c3 = manager.get_client({"kind": "direct"})
        
        self.assertIs(c1, c2)
        self.assertIs(c2, c3)

        # Check route-profile specific client creation
        proxy_client = manager.get_client({"kind": "http_proxy", "endpoint": "127.0.0.1:8080"})
        self.assertIsNot(proxy_client, c1)

        # Cleanup
        manager.close()
        self.assertEqual(len(manager._clients), 0)

    def test_dynamic_segment_coordinator_work_stealing(self):
        manifest_file = self.work_dir / "test.part.segments.json"
        total_size = 20 * 1024 * 1024  # 20 MB
        initial_ranges = [
            (0, 9 * 1024 * 1024 - 1),  # 9 MB
            (9 * 1024 * 1024, total_size - 1),  # 11 MB
        ]

        coord = DynamicSegmentCoordinator(
            manifest_path=manifest_file,
            total_size=total_size,
            min_steal_bytes=2 * 1024 * 1024,  # 2 MB min steal
        )
        coord.load_or_init(initial_ranges)

        self.assertEqual(len(coord.segments), 2)
        self.assertTrue(coord.manifest_path.exists())

        # Worker 1 claims segment 0
        seg1 = coord.claim_work("worker-1")
        self.assertIsNotNone(seg1)
        self.assertEqual(seg1.id, 0)
        self.assertEqual(seg1.status, "active")
        self.assertEqual(seg1.worker_id, "worker-1")

        # Worker 2 claims segment 1
        seg2 = coord.claim_work("worker-2")
        self.assertIsNotNone(seg2)
        self.assertEqual(seg2.id, 1)
        self.assertEqual(seg2.status, "active")
        self.assertEqual(seg2.worker_id, "worker-2")

        # Worker 1 finishes very quickly
        coord.record_progress(seg1.id, seg1.expected)
        coord.complete_segment(seg1.id)
        self.assertEqual(seg1.status, "completed")

        # Worker 2 has only completed 1 MB of its 11 MB segment (10 MB remaining)
        coord.record_progress(seg2.id, 1024 * 1024)
        self.assertEqual(seg2.remaining, 10 * 1024 * 1024)

        # Worker 1 is idle and attempts to claim work -> should steal from Worker 2!
        stolen_seg = coord.claim_work("worker-1")
        self.assertIsNotNone(stolen_seg)
        self.assertEqual(stolen_seg.id, 2)
        self.assertEqual(stolen_seg.worker_id, "worker-1")
        self.assertEqual(stolen_seg.status, "active")

        # Verify Worker 2's segment was shortened and Worker 1 took the tail
        self.assertEqual(seg2.end + 1, stolen_seg.start)
        self.assertEqual(stolen_seg.end, total_size - 1)
        self.assertEqual(seg2.expected + stolen_seg.expected, 11 * 1024 * 1024)

        # Worker 2 completes its remaining shortened portion
        coord.record_progress(seg2.id, seg2.expected - seg2.done)
        coord.complete_segment(seg2.id)

        # Worker 1 completes the stolen portion
        coord.record_progress(stolen_seg.id, stolen_seg.expected)
        coord.complete_segment(stolen_seg.id)

        self.assertTrue(coord.is_all_done())
        self.assertEqual(coord.total_bytes_done(), total_size)

    def test_dynamic_segment_coordinator_resume(self):
        manifest_file = self.work_dir / "resume.part.segments.json"
        total_size = 10 * 1024 * 1024  # 10 MB
        initial_ranges = [(0, 4999999), (5000000, 9999999)]

        # Initial session
        coord = DynamicSegmentCoordinator(manifest_path=manifest_file, total_size=total_size)
        coord.load_or_init(initial_ranges)
        seg0 = coord.claim_work("w1")
        coord.record_progress(seg0.id, 2_000_000)

        # Simulate restart with new coordinator instance loading the saved manifest
        coord2 = DynamicSegmentCoordinator(manifest_path=manifest_file, total_size=total_size)
        coord2.load_or_init(initial_ranges)

        self.assertEqual(len(coord2.segments), 2)
        self.assertEqual(coord2.segments[0].done, 2_000_000)
        self.assertEqual(coord2.segments[0].status, "pending")  # Active segments reset to pending on resume
        self.assertEqual(coord2.total_bytes_done(), 2_000_000)

    def test_range_planner_classifies_probe_states(self):
        planner = RangePlanner(min_range_size=1024, max_range_size=8192)
        valid = planner.probe_and_classify({
            "status_code": 206,
            "size": 10_000,
            "ranges": True,
            "content_range": "bytes 0-0/10000",
            "headers": {"Content-Length": "1", "ETag": '"fixture-v1"'},
        })
        self.assertEqual(valid.capability, RangeCapability.VALID_RANGED)
        self.assertEqual(valid.validator, '"fixture-v1"')
        # Ranged response without validator (e.g. MEGA CDN) should still be VALID_RANGED
        valid_no_validator = planner.probe_and_classify({
            "status_code": 206,
            "size": 10_000,
            "ranges": True,
            "content_range": "bytes 0-0/10000",
            "headers": {"Content-Length": "1"},
        })
        self.assertEqual(valid_no_validator.capability, RangeCapability.VALID_RANGED)
        self.assertIsNone(valid_no_validator.validator)
        self.assertTrue(valid_no_validator.supports_ranges)
        self.assertEqual(planner.probe_and_classify({"status_code": 200, "size": 10000, "ranges": False}).capability,
                         RangeCapability.SEQUENTIAL_ONLY)
        self.assertEqual(planner.probe_and_classify({
            "status_code": 206, "size": 10000, "ranges": True,
            "content_range": "bytes 1-1/10000", "headers": {"ETag": '"fixture-v1"'},
        }).capability, RangeCapability.AMBIGUOUS)
        self.assertEqual(planner.probe_and_classify({
            "status_code": 416, "content_range": "bytes */10000", "ranges": False,
            "headers": {"ETag": '"fixture-v1"'},
        }).capability, RangeCapability.INVALID_416)
        self.assertEqual(planner.probe_and_classify({
            "status_code": 206, "size": 10000, "ranges": True,
            "content_range": "bytes 0-0/10000", "headers": {"ETag": '"fixture-v2"'},
        }, expected_validator='"fixture-v1"').capability, RangeCapability.VALIDATOR_CONFLICT)

    def test_range_planner_adapts_future_ranges_without_rewriting_initial(self):
        planner = RangePlanner(min_range_size=1024, max_range_size=8192, initial_concurrency=2, max_concurrency=4)
        plan = planner.plan_initial_ranges(100_000)
        original = list(plan.ranges)
        future = planner.next_range(plan.ranges[-1][1] + 1, 100_000, observed_bytes_per_second=4096)
        self.assertEqual(plan.ranges, original)
        self.assertIsNotNone(future)
        self.assertGreaterEqual(future[1] - future[0] + 1, 8192)

    def test_in_flight_checksum_validation(self):
        target = self.work_dir / "verified_file.bin"
        content = b"High-speed accelerated download test payload" * 100
        target.write_bytes(content)

        expected_sha256 = hashlib.sha256(content).hexdigest()
        item = ResolvedItem(
            provider="generic",
            source_url="https://example.com/test.bin",
            display_name="verified_file.bin",
            direct_url="https://example.com/test.bin",
            size=len(content),
            checksum=f"sha256:{expected_sha256}",
        )

        # Test with correct in-flight digest (bypasses reading from disk)
        CustomAsyncBackend._verify(target, item, expected_size=len(content), in_flight_digest=expected_sha256)

        # Test mismatch in in-flight digest
        with self.assertRaises(RuntimeError):
            CustomAsyncBackend._verify(target, item, expected_size=len(content), in_flight_digest="deadbeef" * 8)

        # Test disk fallback when in_flight_digest is None
        CustomAsyncBackend._verify(target, item, expected_size=len(content), in_flight_digest=None)

    def test_custom_async_backend_segmented_end_to_end(self):
        resources = ResourceManager(
            policy=SchedulerPolicy(
                min_segment_size=1024,
                max_segments_per_file=4,
                max_retries=1,
            )
        )
        backend = CustomAsyncBackend(resources=resources)

        content = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 200  # 7200 bytes
        total_size = len(content)
        expected_hash = hashlib.sha256(content).hexdigest()

        item = ResolvedItem(
            provider="generic",
            source_url="http://test.local/file.bin",
            direct_url="http://test.local/file.bin",
            size=total_size,
            checksum=f"sha256:{expected_hash}",
            display_name="file.bin",
        )

        # Mock probe to report range support
        probe_meta = {
            "size": total_size,
            "ranges": True,
            "status_code": 206,
            "content_range": f"bytes 0-0/{total_size}",
            "headers": {"Content-Length": "1", "ETag": '"fixture-v1"'},
            "etag": '"fixture-v1"',
        }
        
        # Mock range fetching
        def mock_fetch(item_arg, path_arg, seg_arg, start, end, control, loop, route_profile, coordinator, report_cb):
            chunk = content[seg_arg.start:seg_arg.end + 1]
            with path_arg.open("r+b") as h:
                h.seek(seg_arg.start)
                h.write(chunk)
            coordinator.record_progress(seg_arg.id, len(chunk))
            asyncio.run_coroutine_threadsafe(report_cb(len(chunk)), loop).result()
            return len(chunk)

        progress_reports = []

        with patch.object(backend, "_probe", return_value=probe_meta), \
             patch.object(backend, "_fetch_range_stealer", side_effect=mock_fetch):
            dest = asyncio.run(
                backend.download(
                    item=item,
                    root=self.work_dir,
                    progress=lambda p: progress_reports.append(p),
                )
            )

            self.assertTrue(dest.exists())
            self.assertEqual(dest.stat().st_size, total_size)
            self.assertEqual(dest.read_bytes(), content)
            self.assertTrue(len(progress_reports) > 0)
            self.assertEqual(progress_reports[-1], total_size)

    def test_keepalive_socket_terminates_immediately_at_expected_wire(self):
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        payload = b"X" * 4096

        class _KeepaliveHoldHandler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "keep-alive")
                self.end_headers()

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                # Server keeps socket open; if client performs trailing read, block for 5s
                time.sleep(5.0)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _KeepaliveHoldHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            resources = ResourceManager(SchedulerPolicy(bandwidth_bytes_per_second=0))
            backend = CustomAsyncBackend(resources)
            item = ResolvedItem(
                provider="generic",
                source_url=f"http://127.0.0.1:{server.server_port}/file.bin",
                display_name="file.bin",
                size=len(payload),
                direct_url=f"http://127.0.0.1:{server.server_port}/file.bin",
            )
            t0 = time.time()
            dest = asyncio.run(backend.download(item=item, root=self.work_dir))
            duration = time.time() - t0
            # Must finish well before the 5.0 second server sleep
            self.assertLess(duration, 2.0)
            self.assertEqual(dest.read_bytes(), payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_truncated_stream_raises_ioerror(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _TruncatedHandler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", "8192")
                self.end_headers()

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "8192")
                self.end_headers()
                self.wfile.write(b"A" * 2048)
                self.wfile.flush()
                # Abruptly close connection without sending remaining 6144 bytes
                self.close_connection = True

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _TruncatedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            resources = ResourceManager(SchedulerPolicy(bandwidth_bytes_per_second=0))
            backend = CustomAsyncBackend(resources)
            item = ResolvedItem(
                provider="generic",
                source_url=f"http://127.0.0.1:{server.server_port}/truncated.bin",
                display_name="truncated.bin",
                size=8192,
                direct_url=f"http://127.0.0.1:{server.server_port}/truncated.bin",
            )
            with self.assertRaises(Exception) as ctx:
                asyncio.run(backend.download(item=item, root=self.work_dir))
            err_msg = str(ctx.exception).lower()
            self.assertTrue(any(term in err_msg for term in ("truncated", "missing", "transfer closed", "curl: (18)")),
                            f"Unexpected truncation error message: {err_msg}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
