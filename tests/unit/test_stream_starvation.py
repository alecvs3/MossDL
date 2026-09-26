"""A slow lane is not a starved lane.

We originally read "one stream fast, two streams crawling" on DataNodes as the
host accepting surplus connections and then starving them, and built a detector
that dropped and requeued the slow ones and demoted the host's ceiling to the
productive count. That diagnosis was wrong, and the machinery built on it is
what collapsed multipart packages to one stream at a time.

DataNodes' CDN assigns a lane per session. Some lanes are slow. A slow lane
still finishes, and the lane is bound to the session -- so a dropped stream
re-resolves onto the same slow lane, paying a full re-solve for nothing. A
reference client that reliably runs four DataNodes parts at once encodes exactly
this (clone_reference/moondownloader/moon_download.py:33): "re-extracting returns
the same slow lane. A slow file WILL finish."

The policy these tests pin is therefore deliberately reluctant: judge sustained
rate over a window rather than bytes-so-far, allow a long grace, never touch a
stream that is nearly done or too small to measure, kill at most once, and never
let any of it move the host's ceiling.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from engine.storage_concurrency import (
    _STALL_GRACE_SECONDS,
    _STALL_WINDOW_SECONDS,
    StorageHostConcurrencyManager,
)

HOST = "node42.datanodes.to"
PREAMBLE_BYTES = 7835
MB = 1024 * 1024
GB = 1024 * MB

PAST_GRACE = _STALL_GRACE_SECONDS + 30
FULL_WINDOW = _STALL_WINDOW_SECONDS + 5


class DeadStreamDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = StorageHostConcurrencyManager(Path(self.tmp.name))

    def _stream(self, task_id: str, *, total: int, speed: float, age: float,
                window_bytes: int | None = None, window_age: float = FULL_WINDOW,
                expected: int = 2 * GB) -> None:
        """A stream that has moved `total - window_bytes` over `window_age`."""
        self.mgr.register_stream(HOST, task_id, expected_bytes=expected)
        s = self.mgr._active_streams[HOST][task_id]
        now = time.time()
        s.total_bytes = total
        s.speed_bps = speed
        s.started_at = now - age
        s.window_started_at = now - window_age
        s.window_start_bytes = total if window_bytes is None else window_bytes

    def test_a_stream_moving_nothing_at_all_is_killed(self) -> None:
        """The case the policy still exists for: genuinely dead, not slow."""
        self._stream("productive", total=500 * MB, speed=60 * MB, age=PAST_GRACE,
                     window_bytes=100 * MB)
        self._stream("dead", total=PREAMBLE_BYTES, speed=0.0, age=PAST_GRACE)
        self.assertEqual(self.mgr.check_zero_byte_stalls(HOST), ["dead"])

    def test_a_slow_lane_is_left_alone(self) -> None:
        """The regression this whole file exists to prevent.

        2 MB/s is miserable next to a 60 MB/s sibling, and it is exactly what a
        slow DataNodes lane looks like. Killing it re-resolves onto the same
        lane and costs a full solve.
        """
        self._stream("productive", total=500 * MB, speed=60 * MB, age=PAST_GRACE,
                     window_bytes=100 * MB)
        slow_moved = int(2 * MB * FULL_WINDOW)
        self._stream("slow", total=slow_moved, speed=2 * MB, age=PAST_GRACE, window_bytes=0)
        self.assertEqual(
            self.mgr.check_zero_byte_stalls(HOST), [],
            "killed a slow-but-advancing lane; it would have finished")

    def test_a_lane_that_previously_moved_then_died_is_detected(self) -> None:
        """Lifetime bytes must not make a currently dead lane immortal."""
        self._stream("productive", total=500 * MB, speed=60 * MB, age=PAST_GRACE,
                     window_bytes=100 * MB)
        self._stream("died", total=40 * MB, speed=0.0, age=PAST_GRACE,
                     window_bytes=40 * MB)
        self.assertEqual(self.mgr.check_zero_byte_stalls(HOST), ["died"])

    def test_live_updates_preserve_a_full_watchdog_window(self) -> None:
        """Crossing 60s must not erase the evidence before inspection."""
        self.mgr.register_stream(HOST, "dead", expected_bytes=2 * GB)
        stream = self.mgr._active_streams[HOST]["dead"]
        now = time.time()
        stream.started_at = now - PAST_GRACE
        stream.window_samples.clear()
        stream.window_samples.extend([
            (now - FULL_WINDOW, PREAMBLE_BYTES),
            (now, PREAMBLE_BYTES),
        ])
        stream.window_started_at = now - FULL_WINDOW
        stream.window_start_bytes = PREAMBLE_BYTES
        self.mgr.update_stream(HOST, "dead", PREAMBLE_BYTES, 0.0)
        _rate, window = stream.window_rate_bps(time.time())
        self.assertGreaterEqual(window, _STALL_WINDOW_SECONDS)

    def test_a_stream_inside_the_grace_period_is_left_alone(self) -> None:
        self._stream("productive", total=500 * MB, speed=60 * MB, age=PAST_GRACE,
                     window_bytes=100 * MB)
        self._stream("young", total=PREAMBLE_BYTES, speed=0.0, age=_STALL_GRACE_SECONDS - 10)
        self.assertEqual(self.mgr.check_zero_byte_stalls(HOST), [])

    def test_a_short_window_is_not_enough_evidence(self) -> None:
        self._stream("productive", total=500 * MB, speed=60 * MB, age=PAST_GRACE,
                     window_bytes=100 * MB)
        self._stream("unjudged", total=PREAMBLE_BYTES, speed=0.0, age=PAST_GRACE,
                     window_age=_STALL_WINDOW_SECONDS - 10)
        self.assertEqual(self.mgr.check_zero_byte_stalls(HOST), [])

    def test_a_nearly_finished_stream_is_never_killed(self) -> None:
        """Restarting at 90% throws away real work to chase a stall."""
        self._stream("productive", total=500 * MB, speed=60 * MB, age=PAST_GRACE,
                     window_bytes=100 * MB)
        self._stream("almost", total=int(2 * GB * 0.9), speed=0.0, age=PAST_GRACE,
                     window_bytes=int(2 * GB * 0.9), expected=2 * GB)
        self.assertEqual(self.mgr.check_zero_byte_stalls(HOST), [])

    def test_a_small_file_is_not_judged(self) -> None:
        self._stream("productive", total=500 * MB, speed=60 * MB, age=PAST_GRACE,
                     window_bytes=100 * MB)
        self._stream("tiny", total=1024, speed=0.0, age=PAST_GRACE, expected=10 * MB)
        self.assertEqual(self.mgr.check_zero_byte_stalls(HOST), [])

    def test_a_stream_is_killed_at_most_once(self) -> None:
        """A second kill means an unbounded drop/requeue loop."""
        self._stream("productive", total=500 * MB, speed=60 * MB, age=PAST_GRACE,
                     window_bytes=100 * MB)
        self._stream("dead", total=PREAMBLE_BYTES, speed=0.0, age=PAST_GRACE)
        self.assertEqual(self.mgr.check_zero_byte_stalls(HOST), ["dead"])

        self.mgr.unregister_stream(HOST, "dead")
        self._stream("dead", total=PREAMBLE_BYTES, speed=0.0, age=PAST_GRACE)
        self.assertEqual(
            self.mgr.check_zero_byte_stalls(HOST), [],
            "killed the same task twice; it must be allowed to finish however slowly")

    def test_nothing_is_killed_without_a_productive_sibling(self) -> None:
        self._stream("a", total=PREAMBLE_BYTES, speed=0.0, age=PAST_GRACE)
        self._stream("b", total=PREAMBLE_BYTES, speed=0.0, age=PAST_GRACE)
        self.assertEqual(self.mgr.check_zero_byte_stalls(HOST), [])

    def test_a_single_stream_is_never_killed(self) -> None:
        self._stream("only", total=0, speed=0.0, age=PAST_GRACE * 2)
        self.assertEqual(self.mgr.check_zero_byte_stalls(HOST), [])


class DeadStreamCountTests(unittest.TestCase):
    """What the probe logic is allowed to hold against a host."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = StorageHostConcurrencyManager(Path(self.tmp.name))

    def _stream(self, task_id: str, *, total: int, window_bytes: int, age: float = PAST_GRACE) -> None:
        self.mgr.register_stream(HOST, task_id)
        s = self.mgr._active_streams[HOST][task_id]
        now = time.time()
        s.total_bytes = total
        s.started_at = now - age
        s.window_started_at = now - FULL_WINDOW
        s.window_start_bytes = window_bytes

    def test_slow_lanes_do_not_count_as_dead(self) -> None:
        """Otherwise a host with a permanently slow lane never gets probed."""
        self._stream("slow", total=int(2 * MB * FULL_WINDOW), window_bytes=0)
        self.assertEqual(self.mgr.dead_stream_count(HOST), 0)

    def test_a_stream_moving_nothing_counts_as_dead(self) -> None:
        self._stream("dead", total=PREAMBLE_BYTES, window_bytes=PREAMBLE_BYTES)
        self.assertEqual(self.mgr.dead_stream_count(HOST), 1)

    def test_a_young_stream_is_not_counted(self) -> None:
        self._stream("young", total=0, window_bytes=0, age=5)
        self.assertEqual(self.mgr.dead_stream_count(HOST), 0)


class ProductiveAccountingTests(unittest.TestCase):
    """Reporting only. `productive_stream_count` describes throughput; it must
    never be used to decide a host's ceiling."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = StorageHostConcurrencyManager(Path(self.tmp.name))

    def _stream(self, task_id: str, total: int, speed: float) -> None:
        self.mgr.register_stream(HOST, task_id)
        stream = self.mgr._active_streams[HOST][task_id]
        stream.total_bytes = total
        stream.speed_bps = speed

    def test_open_streams_are_not_productive_streams(self) -> None:
        self._stream("p2", 500 * MB, 60 * MB)
        self._stream("p3", PREAMBLE_BYTES, 0.0)
        self._stream("p4", PREAMBLE_BYTES, 0.0)
        self.assertEqual(len(self.mgr._active_streams[HOST]), 3)
        self.assertEqual(self.mgr.productive_stream_count(HOST), 1)

    def test_aggregate_speed_sums_the_host(self) -> None:
        self._stream("a", 100 * MB, 30 * MB)
        self._stream("b", 100 * MB, 20 * MB)
        self.assertAlmostEqual(self.mgr.aggregate_speed_bps(HOST) / MB, 50.0, places=3)

    def test_aggregate_is_zero_for_an_unknown_host(self) -> None:
        self.assertEqual(self.mgr.aggregate_speed_bps("never-seen.example"), 0.0)

    def test_interval_goodput_uses_one_clock_for_every_stream(self) -> None:
        self.mgr.register_stream(HOST, "a")
        self.mgr.register_stream(HOST, "b")
        a = self.mgr._active_streams[HOST]["a"]
        b = self.mgr._active_streams[HOST]["b"]
        baseline = max(a.sample_at, b.sample_at)
        a.sample_at = b.sample_at = baseline
        a.total_bytes = 30 * MB
        b.total_bytes = 20 * MB

        sample = self.mgr.sample_host_goodput(HOST, now=baseline + 1.0)

        self.assertAlmostEqual(sample["aggregate_speed_bps"] / MB, 50.0, places=3)
        self.assertEqual(sample["productive"], 2)
        self.assertEqual([row["interval_bytes"] for row in sample["streams"]], [30 * MB, 20 * MB])

    def test_silent_stream_decays_to_zero_on_next_interval(self) -> None:
        self.mgr.register_stream(HOST, "lane")
        stream = self.mgr._active_streams[HOST]["lane"]
        baseline = stream.sample_at
        stream.total_bytes = 10 * MB
        first = self.mgr.sample_host_goodput(HOST, now=baseline + 1.0)
        second = self.mgr.sample_host_goodput(HOST, now=baseline + 2.0)

        self.assertAlmostEqual(first["aggregate_speed_bps"] / MB, 10.0, places=3)
        self.assertEqual(second["aggregate_speed_bps"], 0.0)
        self.assertEqual(second["productive"], 0)


class ProbeWithholdTests(unittest.TestCase):
    """Withhold a probe for a dead stream, never for a merely slow one."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = StorageHostConcurrencyManager(Path(self.tmp.name))
        self.asked: list[tuple] = []

        class _Auditor:
            def probe_allowance(inner, host, current_limit, demand=0):
                self.asked.append((host, current_limit, demand))
                return current_limit + 1

        self.mgr._auditor_ref = lambda: _Auditor()

    def _saturate(self, *, fast: int = 0, slow: int = 0, dead: int = 0) -> None:
        from engine.storage_concurrency import ResizableSemaphore

        sem = ResizableSemaphore(1)
        sem._in_use = 1
        sem._waiters = 3
        self.mgr._semaphores[HOST] = sem
        now = time.time()

        def add(task_id: str, moved: int) -> None:
            self.mgr.register_stream(HOST, task_id)
            s = self.mgr._active_streams[HOST][task_id]
            s.started_at = now - PAST_GRACE
            s.window_started_at = now - FULL_WINDOW
            s.window_start_bytes = 0
            s.total_bytes = moved
            s.speed_bps = moved / FULL_WINDOW

        for i in range(fast):
            add(f"fast-{i}", int(50 * MB * FULL_WINDOW))
        for i in range(slow):
            add(f"slow-{i}", int(2 * MB * FULL_WINDOW))
        for i in range(dead):
            add(f"dead-{i}", 0)

    def test_probe_is_withheld_while_a_stream_is_dead(self) -> None:
        self._saturate(fast=1, dead=2)
        self.mgr.reconsider_limits()
        self.assertEqual(self.asked, [], "opened another lane while two streams were dead")

    def test_probe_proceeds_alongside_a_slow_lane(self) -> None:
        """The pin: a permanently slow DataNodes lane must not block escalation."""
        self._saturate(fast=1, slow=2)
        self.mgr.reconsider_limits()
        self.assertEqual(
            len(self.asked), 1,
            "a slow lane withheld the probe, so the ceiling could never rise")

    def test_probe_proceeds_when_every_stream_is_fast(self) -> None:
        self._saturate(fast=2)
        self.mgr.reconsider_limits()
        self.assertEqual(len(self.asked), 1)


if __name__ == "__main__":
    unittest.main()
