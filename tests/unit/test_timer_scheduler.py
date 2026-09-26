"""Unit tests for ranked timer detection and the cross-part TimerScheduler.

Covers: ranked-candidate parsing (Vue prop, data attr, JS var, JSON wait, text
wait, decoys), two-sample arming, skip-on-clearance, concurrent deadlines,
re-arm, telemetry tags, and proof that waits use the awaited async primitive
instead of a thread-blocking sleep loop.
"""

import asyncio
import inspect
import threading
import time
import unittest

from engine.timer_detector import TimerDetector
from engine.timer_scheduler import TimerScheduler


class RecordingTelemetry:
    """Minimal telemetry sink capturing structured events for assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict]] = []

    def record(self, level: str, subsystem: str, message: str, context=None, **kwargs) -> int:
        self.events.append((level, subsystem, message, dict(context or {})))
        return len(self.events)

    def has(self, tag: str) -> bool:
        return any(f"[{tag}]" in message for _, _, message, _ in self.events)

    def contexts(self, tag: str) -> list[dict]:
        return [ctx for _, _, message, ctx in self.events if f"[{tag}]" in message]


class TestRankedCandidateParsing(unittest.TestCase):
    def test_vue_prop(self):
        html = '<download-countdown :countdown="10" :has-countdown="true"></download-countdown>'
        best = TimerDetector.best_candidate(TimerDetector.detect_candidates(html))
        self.assertIsNotNone(best)
        self.assertEqual(best.source, "vue_prop")
        self.assertEqual(best.seconds, 10)

    def test_data_attribute(self):
        html = '<div class="download-box" data-timer="25">Please wait...</div>'
        best = TimerDetector.best_candidate(TimerDetector.detect_candidates(html))
        self.assertEqual(best.source, "data_attribute")
        self.assertEqual(best.seconds, 25)

    def test_js_variable(self):
        html = "<script>var countdown = 14;</script>"
        best = TimerDetector.best_candidate(TimerDetector.detect_candidates(html))
        self.assertEqual(best.source, "js_variable")
        self.assertEqual(best.seconds, 14)

    def test_json_wait_embedded(self):
        html = '<script>window.__timer = {"wait": 30};</script>'
        best = TimerDetector.best_candidate(TimerDetector.detect_candidates(html))
        self.assertEqual(best.source, "json_wait")
        self.assertEqual(best.seconds, 30)

    def test_json_wait_pure_body(self):
        body = '{"status": "wait", "retry_after": 12}'
        best = TimerDetector.best_candidate(TimerDetector.detect_candidates(body))
        self.assertEqual(best.source, "json_wait")
        self.assertEqual(best.seconds, 12)

    def test_text_wait_seconds_and_minutes(self):
        best_secs = TimerDetector.best_candidate(TimerDetector.detect_candidates("Please wait 45 seconds"))
        self.assertEqual(best_secs.source, "text_wait")
        self.assertEqual(best_secs.seconds, 45)

        best_min = TimerDetector.best_candidate(TimerDetector.detect_candidates("Try again in 2 minutes"))
        self.assertEqual(best_min.source, "text_wait")
        self.assertEqual(best_min.seconds, 120)

        best_bare = TimerDetector.best_candidate(TimerDetector.detect_candidates("wait 10"))
        self.assertEqual(best_bare.source, "text_wait")
        self.assertEqual(best_bare.seconds, 10)

    def test_headers_outrank_html(self):
        html = '<download-countdown :countdown="10"></download-countdown>'
        best = TimerDetector.best_candidate(TimerDetector.detect_candidates(html, headers={"Retry-After": "18"}))
        self.assertEqual(best.source, "http_header")
        self.assertEqual(best.seconds, 18)

    def test_decoys_are_ranked_last_but_not_dropped(self):
        html = (
            '<div data-timer="0">Ready</div>'
            '<div data-timer="999999">never</div>'
            '<span class="countdown">--:--</span>'
            '<download-countdown :countdown="10"></download-countdown>'
        )
        candidates = TimerDetector.detect_candidates(html)
        best = TimerDetector.best_candidate(candidates)
        self.assertEqual(best.source, "vue_prop")
        self.assertEqual(best.seconds, 10)
        self.assertTrue(any(c.seconds == 999999 and not c.valid for c in candidates))

    def test_multiple_sources_not_silently_dropped(self):
        html = (
            '<download-countdown :countdown="10"></download-countdown>'
            "<script>var countdown = 9;</script>"
            "<p>Please wait 8 seconds</p>"
        )
        candidates = TimerDetector.detect_candidates(html)
        sources = {c.source for c in candidates}
        self.assertIn("vue_prop", sources)
        self.assertIn("js_variable", sources)
        self.assertIn("text_wait", sources)
        self.assertGreaterEqual(len(candidates), 3)

    def test_detect_from_html_wrapper_returns_best(self):
        html = (
            '<download-countdown :countdown="10"></download-countdown>'
            "<script>var countdown = 99;</script>"
        )
        result = TimerDetector.detect_from_html(html)
        self.assertTrue(result.has_timer)
        self.assertEqual(result.seconds_remaining, 10)
        self.assertEqual(result.detection_source, "vue_prop")


class TestTwoSampleArming(unittest.TestCase):
    def setUp(self) -> None:
        self.telemetry = RecordingTelemetry()
        self.scheduler = TimerScheduler(telemetry=self.telemetry)

    def test_arms_only_after_two_decreasing_samples(self):
        first = TimerDetector.detect_candidates('<div data-timer="10"></div>')
        self.assertIsNone(self.scheduler.observe("datanodes.to", "part1", first))
        self.assertFalse(self.scheduler.is_armed("datanodes.to", "part1"))

        repeated = TimerDetector.detect_candidates('<div data-timer="10"></div>')
        self.assertIsNone(self.scheduler.observe("datanodes.to", "part1", repeated))
        self.assertFalse(self.scheduler.is_armed("datanodes.to", "part1"))

        decreased = TimerDetector.detect_candidates('<div data-timer="9"></div>')
        state = self.scheduler.observe("datanodes.to", "part1", decreased)
        self.assertIsNotNone(state)
        self.assertTrue(self.scheduler.is_armed("datanodes.to", "part1"))
        self.assertEqual(state.seconds, 9)
        self.assertAlmostEqual(state.deadline - state.armed_at, 9.0, places=3)
        self.assertTrue(self.telemetry.has("TIMER_ARMED"))
        self.assertTrue(self.telemetry.has("TIMER_CANDIDATES"))

    def test_static_decoy_never_arms(self):
        decoy = TimerDetector.detect_candidates('<div data-timer="25">Ready</div>')
        for _ in range(5):
            self.scheduler.observe("host.test", "p2", decoy)
        self.assertFalse(self.scheduler.is_armed("host.test", "p2"))

    def test_parse_miss_is_loud(self):
        self.assertIsNone(self.scheduler.observe("host.test", "p3", []))
        self.assertTrue(self.telemetry.has("TIMER_PARSE_MISS"))


class TestSchedulerDecisions(unittest.TestCase):
    def setUp(self) -> None:
        self.telemetry = RecordingTelemetry()
        self.scheduler = TimerScheduler(telemetry=self.telemetry)

    def _candidates(self, seconds: int):
        return TimerDetector.detect_candidates(f'<div data-timer="{seconds}"></div>')

    def test_skip_on_clearance(self):
        for reason in ("cf_clearance", "api_key", "direct_url"):
            state = self.scheduler.observe("datanodes.to", "part1", self._candidates(30), satisfied_reason=reason)
            self.assertIsNone(state)
            self.assertFalse(self.scheduler.is_armed("datanodes.to", "part1"))
        self.assertGreaterEqual(len(self.telemetry.contexts("TIMER_SKIP")), 3)
        reasons = {ctx.get("reason") for ctx in self.telemetry.contexts("TIMER_SKIP")}
        self.assertTrue({"cf_clearance", "api_key", "direct_url"}.issubset(reasons))

    def test_rearm_after_fresh_solve(self):
        self.scheduler.arm("datanodes.to", "part1", 5, source="vue_prop")
        first = self.scheduler.armed_state("datanodes.to", "part1")
        state = self.scheduler.rearm(
            "datanodes.to", "part1", seconds=7, reason="fresh_solve_retry",
        )
        self.assertIsNotNone(state)
        self.assertEqual(state.seconds, 7)
        self.assertEqual(state.reason, "fresh_solve_retry")
        self.assertIsNot(state, first)
        self.assertGreater(state.deadline, first.deadline)

        state_from_candidates = self.scheduler.rearm("datanodes.to", "part1", candidates=self._candidates(3))
        self.assertEqual(state_from_candidates.seconds, 3)
        self.assertEqual(state_from_candidates.source, "data_attribute")

    def test_rearm_resets_two_sample_history(self):
        self.scheduler.observe("host.test", "p1", self._candidates(10))
        state = self.scheduler.rearm("host.test", "p1", seconds=4)
        self.assertIsNotNone(state)
        self.assertTrue(self.scheduler.is_armed("host.test", "p1"))

    def test_satisfied_early_clears_deadline(self):
        self.scheduler.arm("host.test", "p1", 30)
        self.scheduler.satisfied("host.test", "p1", "already_elapsed")
        self.assertFalse(self.scheduler.is_armed("host.test", "p1"))
        self.assertTrue(self.telemetry.has("TIMER_SATISFIED_EARLY"))

    def test_rate_limited_arms_and_reports(self):
        state = self.scheduler.note_rate_limited("host.test", "p1", seconds=12, status=429)
        self.assertIsNotNone(state)
        self.assertEqual(state.source, "rate_limit")
        self.assertEqual(state.seconds, 12)
        self.assertTrue(self.telemetry.has("TIMER_RATE_LIMITED"))

    def test_safety_cap_emits_telemetry(self):
        scheduler = TimerScheduler(telemetry=self.telemetry, max_wait_seconds=30)
        state = scheduler.arm("host.test", "p1", 3600)
        self.assertEqual(state.seconds, 3600, 'local budgets must not shorten server deadlines')
        self.assertTrue(state.capped)
        self.assertTrue(self.telemetry.has("TIMER_CAPPED"))

    def test_non_positive_arm_is_reported_not_silent(self):
        self.assertIsNone(self.scheduler.arm("host.test", "p1", 0))
        self.assertTrue(self.telemetry.has("TIMER_SKIP"))


class TestAsyncWaiting(unittest.TestCase):
    def setUp(self) -> None:
        self.telemetry = RecordingTelemetry()

    def test_wait_primitives_are_coroutine_functions(self):
        self.assertTrue(inspect.iscoroutinefunction(TimerScheduler.wait_until))
        self.assertTrue(inspect.iscoroutinefunction(TimerScheduler.wait_for))

    def test_wait_until_is_cooperatively_awaited(self):
        scheduler = TimerScheduler(telemetry=self.telemetry, default_tick_interval=0.02)
        partner_ran: list[str] = []

        async def main() -> bool:
            async def partner() -> None:
                for _ in range(3):
                    partner_ran.append("tick")
                    await asyncio.sleep(0.02)

            results = await asyncio.gather(
                scheduler.wait_until(time.monotonic() + 0.1, host="h", key="p1", tick_interval=0.02),
                partner(),
            )
            return results[0]

        completed = asyncio.run(main())
        self.assertTrue(completed)
        self.assertGreaterEqual(len(partner_ran), 3, "event loop must not be blocked while waiting")
        self.assertTrue(self.telemetry.has("TIMER_TICK"))

    def test_concurrent_deadlines_run_down_together(self):
        scheduler = TimerScheduler(telemetry=self.telemetry, default_tick_interval=0.02)
        scheduler.arm("datanodes.to", "part1", 1)
        scheduler.arm("datanodes.to", "part2", 1)

        async def main():
            started = time.monotonic()
            results = await asyncio.gather(
                scheduler.wait_for("datanodes.to", "part1", tick_interval=0.02),
                scheduler.wait_for("datanodes.to", "part2", tick_interval=0.02),
            )
            return time.monotonic() - started, results

        elapsed, results = asyncio.run(main())
        self.assertEqual(results, [True, True])
        self.assertFalse(scheduler.is_armed("datanodes.to", "part1"))
        self.assertFalse(scheduler.is_armed("datanodes.to", "part2"))
        # Two 1s deadlines overlapping must finish in ~1s, not the ~2s serial sum.
        self.assertLess(elapsed, 1.7)

    def test_wait_until_cancellable_via_event(self):
        scheduler = TimerScheduler(telemetry=self.telemetry, default_tick_interval=0.02)
        cancel = threading.Event()
        cancel.set()
        completed = asyncio.run(
            scheduler.wait_until(time.monotonic() + 5.0, host="h", key="p1", tick_interval=0.02, cancel_event=cancel)
        )
        self.assertFalse(completed)
        self.assertTrue(self.telemetry.has("TIMER_SKIP"))

    def test_sync_facade_bridges_runtime_loop(self):
        scheduler = TimerScheduler(telemetry=self.telemetry, default_tick_interval=0.02)
        try:
            started = time.monotonic()
            completed = scheduler.wait_until_sync(time.monotonic() + 0.1, host="h", key="p1", tick_interval=0.02)
            elapsed = time.monotonic() - started
            self.assertTrue(completed)
            self.assertLess(elapsed, 2.0)
            self.assertTrue(self.telemetry.has("TIMER_TICK"))
        finally:
            scheduler.close()

    def test_sync_facade_runs_multipart_waits_concurrently(self):
        scheduler = TimerScheduler(default_tick_interval=0.02)
        results: list[tuple[str, bool]] = []
        results_lock = threading.Lock()

        def worker(part: str) -> None:
            scheduler.arm("datanodes.to", part, 1)
            ok = scheduler.wait_for_sync("datanodes.to", part, tick_interval=0.02)
            with results_lock:
                results.append((part, ok))

        threads = [threading.Thread(target=worker, args=(f"part{i}",)) for i in range(3)]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - started
        try:
            self.assertEqual(len(results), 3)
            self.assertTrue(all(ok for _, ok in results))
            # Three 1s waits overlap on the runtime loop instead of serializing to ~3s.
            self.assertLess(elapsed, 2.2)
        finally:
            scheduler.close()


class TestHostGateAndNoBlockingLoop(unittest.TestCase):
    def test_host_gate_serializes_final_trigger(self):
        scheduler = TimerScheduler()
        concurrent = 0
        max_concurrent = 0
        counter_lock = threading.Lock()

        def worker() -> None:
            nonlocal concurrent, max_concurrent
            with scheduler.host_gate("datanodes.to"):
                with counter_lock:
                    concurrent += 1
                    max_concurrent = max(max_concurrent, concurrent)
                time.sleep(0.03)
                with counter_lock:
                    concurrent -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(max_concurrent, 1)

    def test_scheduler_module_has_no_blocking_sleep(self):
        import engine.timer_scheduler as timer_scheduler_module

        source = inspect.getsource(timer_scheduler_module)
        self.assertNotIn("time.sleep(", source)

    def test_datanodes_provider_uses_scheduler_wait(self):
        from engine.providers.cyberdrop_hosts import DatanodesProvider

        source = inspect.getsource(DatanodesProvider.resolve)
        self.assertNotIn("time.sleep(", source)
        self.assertNotIn("while wait_remain > 0", source)
        self.assertIn("timer_scheduler.wait_for_sync", source)
        self.assertIn("timer_scheduler.rearm", source)
        self.assertIn("host_gate", source)


if __name__ == "__main__":
    unittest.main()
