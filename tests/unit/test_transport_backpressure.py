from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import shutil
import time
import unittest
from email.utils import formatdate
from unittest.mock import patch

from engine.custom_downloader import CustomAsyncBackend, TransportRetryError
from engine.limits import ResourceManager, SchedulerPolicy
from engine.models import ResolvedItem
from engine.reliability import (
    FailureClass,
    TransportSignalError,
    classify_failure,
    classify_transport_signal,
    decide_retry,
    decorrelated_jitter,
    parse_retry_after,
)
from engine.rust_backend import signal_from_core_error


def _flatten(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        result: list[BaseException] = []
        for child in error.exceptions:
            result.extend(_flatten(child))
        return result
    return [error]


class TransportBackpressureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / ".test-artifacts" / "transport-backpressure" / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_429_retry_after_delta_seconds(self) -> None:
        signal = classify_transport_signal(429, "Too Many Requests", {"Retry-After": "42"})
        self.assertEqual(signal.category, "throttle")
        self.assertTrue(signal.retryable)
        self.assertEqual(signal.status_code, 429)
        self.assertEqual(signal.retry_after_seconds, 42.0)
        self.assertEqual(parse_retry_after(" 2.5 "), 2.5)

    def test_retry_after_http_date_and_invalid_values(self) -> None:
        future = time.time() + 120
        signal = classify_transport_signal(429, "", {"Retry-After": formatdate(future, usegmt=True)})
        self.assertIsNotNone(signal.retry_after_seconds)
        self.assertGreater(signal.retry_after_seconds, 100.0)
        self.assertLessEqual(signal.retry_after_seconds, 125.0)
        self.assertEqual(parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)
        self.assertIsNone(parse_retry_after("not-a-date"))

    def test_403_html_block_page_is_throttle(self) -> None:
        signal = classify_transport_signal(
            403,
            "<html><head><title>Just a moment...</title></head><body>cloudflare</body></html>",
            {"Content-Type": "text/html; charset=UTF-8"},
        )
        self.assertEqual(signal.category, "throttle")
        self.assertTrue(signal.retryable)
        error = TransportSignalError(signal)
        self.assertEqual(classify_failure(error), FailureClass.RATE_LIMITED)
        self.assertTrue(decide_retry(error, 0).retry)

    def test_plain_403_remains_terminal(self) -> None:
        signal = classify_transport_signal(403, "forbidden", {})
        self.assertEqual(signal.category, "terminal")
        self.assertFalse(signal.retryable)

    def test_decorrelated_jitter_bounds(self) -> None:
        self.assertEqual(decorrelated_jitter(0.0), 1.0)
        for value in (decorrelated_jitter(2.0) for _ in range(50)):
            self.assertGreaterEqual(value, 1.0)
            self.assertLessEqual(value, 6.0)
        self.assertEqual(decorrelated_jitter(100.0, base_seconds=1.0, max_seconds=30.0, rng=lambda low, high: high), 30.0)

    def test_throttle_preserves_part_and_releases_permits_before_backoff(self) -> None:
        url = "http://127.0.0.1:9/fixture.bin"
        size = 2 * 1024 * 1024
        probe = {
            "size": size,
            "ranges": True,
            "status_code": 206,
            "range_requested": True,
            "content_range": f"bytes 0-0/{size}",
            "validator": '"fixture-v1"',
            "etag": '"fixture-v1"',
            "last_modified": None,
            "content_type": "application/octet-stream",
        }
        signal = classify_transport_signal(429, "", {})

        def fake_fetch(*_args, **_kwargs):
            raise TransportRetryError(signal)

        async def scenario():
            resources = ResourceManager(SchedulerPolicy(
                max_active_segments=4,
                max_segments_per_file=1,
                per_host_transfers=4,
                initial_segment_concurrency=1,
                min_segment_size=1024,
                bandwidth_bytes_per_second=0,
                max_retries=1,
            ))
            backend = CustomAsyncBackend(resources)
            backend._probe = lambda item, route_profile=None: dict(probe)
            backend._fetch_range_stealer = fake_fetch
            item = ResolvedItem(
                provider="generic",
                source_url=url,
                display_name="fixture.bin",
                relative_path="fixture.bin",
                size=size,
                direct_url=url,
                metadata={"etag": '"fixture-v1"'},
            )
            window = await resources.host_window(url)
            sleep_calls: list[tuple[float, int, int]] = []
            real_sleep = asyncio.sleep

            async def fake_sleep(delay, *args, **kwargs):
                sleep_calls.append((float(delay), resources.active_segments._value, window.in_flight))
                if float(delay) < 0.5:
                    await real_sleep(0)

            with patch.object(asyncio, "sleep", fake_sleep):
                # The bare signal must reach the caller, NOT the ExceptionGroup:
                # a group hides status_code/retry_after, and the caller then
                # classifies the throttle as `unknown` with `status=None` and
                # declines to retry it.
                with self.assertRaises(TransportRetryError) as raised:
                    await backend.download(item, str(self.root))
            return sleep_calls, resources, raised.exception

        sleep_calls, resources, error = asyncio.run(scenario())

        self.assertTrue(any(isinstance(child, TransportRetryError) for child in _flatten(error)))
        self.assertTrue((self.root / "fixture.bin.part").exists())
        self.assertTrue((self.root / "fixture.bin.part.segments.json").exists())
        self.assertEqual((self.root / "fixture.bin.part").stat().st_size, size)

        backoff_sleeps = [entry for entry in sleep_calls if entry[0] >= 0.5]
        self.assertTrue(backoff_sleeps)
        for _delay, available_segments, host_in_flight in backoff_sleeps:
            self.assertEqual(available_segments, resources.policy.max_active_segments)
            self.assertEqual(host_in_flight, 0)

    def test_real_429_range_response_is_classified_and_keeps_part(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        from engine.concurrency_auditor import concurrency_auditor

        size = 2 * 1024 * 1024

        class _ThrottleHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(429)
                self.send_header("Retry-After", "7")
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"slow down")

            def do_HEAD(self):  # noqa: N802
                self.send_response(429)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _ThrottleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/fixture.bin"
            probe = {
                "size": size,
                "ranges": True,
                "status_code": 206,
                "range_requested": True,
                "content_range": f"bytes 0-0/{size}",
                "validator": '"fixture-v1"',
                "etag": '"fixture-v1"',
                "last_modified": None,
                "content_type": "application/octet-stream",
            }

            async def scenario():
                resources = ResourceManager(SchedulerPolicy(
                    max_active_segments=4,
                    max_segments_per_file=1,
                    per_host_transfers=4,
                    initial_segment_concurrency=1,
                    min_segment_size=1024,
                    bandwidth_bytes_per_second=0,
                    max_retries=0,
                ))
                backend = CustomAsyncBackend(resources)
                backend._probe = lambda item, route_profile=None: dict(probe)
                item = ResolvedItem(
                    provider="generic",
                    source_url=url,
                    display_name="fixture.bin",
                    relative_path="fixture.bin",
                    size=size,
                    direct_url=url,
                    metadata={"etag": '"fixture-v1"'},
                )
                # As above: the signal must arrive unwrapped so the retry
                # decision can read its status and Retry-After.
                with self.assertRaises(TransportRetryError) as raised:
                    await backend.download(item, str(self.root))
                return raised.exception

            with patch.object(concurrency_auditor, "record_response_headers", lambda *args, **kwargs: None):
                error = asyncio.run(scenario())

            throttles = [child for child in _flatten(error) if isinstance(child, TransportRetryError)]
            self.assertTrue(throttles)
            self.assertEqual(throttles[0].status_code, 429)
            self.assertEqual(throttles[0].retry_after, 7.0)
            self.assertEqual(throttles[0].transport_signal.category, "throttle")
            self.assertTrue((self.root / "fixture.bin.part").exists())
            self.assertTrue((self.root / "fixture.bin.part.segments.json").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rust_core_error_maps_to_retryable_signal(self) -> None:
        signal = signal_from_core_error("THROTTLED:429:12.5")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.category, "throttle")
        self.assertEqual(signal.status_code, 429)
        self.assertEqual(signal.retry_after_seconds, 12.5)
        error = TransportSignalError(signal, "THROTTLED:429:12.5")
        self.assertEqual(classify_failure(error), FailureClass.RATE_LIMITED)
        decision = decide_retry(error, 0, retry_after=error.retry_after)
        self.assertTrue(decision.retry)
        self.assertEqual(decision.delay_seconds, 12.5)

        retryable = signal_from_core_error("RETRYABLE:503:0 provider returned an HTML error page")
        self.assertIsNotNone(retryable)
        self.assertEqual(retryable.category, "retryable")
        self.assertIsNone(retryable.retry_after_seconds)
        self.assertEqual(classify_failure(TransportSignalError(retryable)), FailureClass.TRANSIENT_NETWORK)
        self.assertIsNone(signal_from_core_error("range retries exhausted"))


if __name__ == "__main__":
    unittest.main()
