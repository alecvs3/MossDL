"""Regression tests for the authoritative host pressure breaker and admission ladder.

Covers: 429 + Retry-After trips, freeze clamping of the auditor ladder and the
AdaptiveHostWindow, probation promotion after sustained successes, probation
failure re-freezing with doubled cooldown, the sticky-clamp reopen regression,
waiter-safe semaphore resize, and scheduler dedupe rejection.
"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from engine.concurrency_auditor import (
    BREAKER_CLOSED,
    BREAKER_FROZEN,
    BREAKER_PROBATION,
    HostConcurrencyAuditor,
    classify_host_pressure,
)
from engine.limits import AdaptiveCircuitBreaker, AdaptiveHostWindow, CircuitOpenError
from engine.scheduler import FairAsyncScheduler
from engine.storage_concurrency import StorageHostConcurrencyManager
from engine.telemetry import telemetry_bus


class HostPressureBreakerTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self._temp.name)
        self.audit_file = self.temp_dir / "audit.jsonl"
        self.auditor = HostConcurrencyAuditor(
            log_path=self.audit_file,
            profiles_path=self.temp_dir / "profiles.json",
        )

    def tearDown(self):
        self._temp.cleanup()

    def events(self) -> list[str]:
        if not self.audit_file.exists():
            return []
        return [
            json.loads(line)["event"]
            for line in self.audit_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _expire_cooldown(self, host: str) -> None:
        profile = self.auditor.get_profile(host)
        profile.cooldown_until = 0.0
        profile.breaker.cooldown_until = time.time() - 1.0

    def test_classify_host_pressure_variants(self):
        self.assertEqual(classify_host_pressure(status_code=429), "http_429")
        self.assertEqual(
            classify_host_pressure(status_code=403, response_text="<html>Attention Required!</html>"),
            "http_403_throttle",
        )
        self.assertEqual(
            classify_host_pressure(error_text="Connection reset by peer", partial_chunks=1),
            "connection_abort_with_partial_chunk",
        )
        self.assertEqual(
            classify_host_pressure(error_text="Cloudflare 524 idle timeout"),
            "cloudflare_idle_timeout",
        )
        self.assertEqual(classify_host_pressure(retry_after=12.0), "retry_after")
        self.assertIsNone(classify_host_pressure(status_code=404, response_text="not found"))

    def test_trip_on_429_retry_after_freezes_host(self):
        host = "trip.example.com"
        profile = self.auditor.get_profile(host)
        profile.verified_ceiling = 4

        reason = self.auditor.note_pressure_failure(
            host, "task-1", status_code=429, retry_after=120.0,
            response_text="too many concurrent downloads",
        )

        self.assertEqual(reason, "retry_after")
        self.assertEqual(profile.breaker.state, BREAKER_FROZEN)
        self.assertEqual(profile.verified_ceiling, 1)
        self.assertGreaterEqual(profile.breaker.cooldown_until - time.time(), 295.0)
        self.assertTrue(self.auditor.is_host_in_cooldown(host))

        allowed, target, because = self.auditor.admission_decision(host, "task-2", active=0)
        self.assertFalse(allowed)
        self.assertEqual(target, 1)
        self.assertIn("frozen", because.lower())
        self.assertIn("BREAKER_TRIPPED", self.events())

    def test_repeat_trips_double_cooldown_capped_at_900(self):
        host = "escalate.example.com"
        profile = self.auditor.get_profile(host)

        self.auditor.note_pressure_failure(host, "t1", status_code=429)
        first = profile.breaker.cooldown_until - time.time()
        self.auditor.note_pressure_failure(host, "t2", status_code=429)
        second = profile.breaker.cooldown_until - time.time()
        self.auditor.note_pressure_failure(host, "t3", status_code=429)
        third = profile.breaker.cooldown_until - time.time()
        self.auditor.note_pressure_failure(host, "t4", status_code=429)
        fourth = profile.breaker.cooldown_until - time.time()

        self.assertAlmostEqual(first, 300.0, delta=5.0)
        self.assertAlmostEqual(second, 600.0, delta=5.0)
        self.assertAlmostEqual(third, 900.0, delta=5.0)
        self.assertAlmostEqual(fourth, 900.0, delta=5.0)
        self.assertEqual(profile.breaker.consecutive_trips, 3)

    def test_retry_after_header_trips_breaker_and_keeps_header_cooldown(self):
        host = "header.example.com"
        now = time.time()
        self.auditor.record_response_headers(host, "task-1", {"Retry-After": "45"})

        profile = self.auditor.get_profile(host)
        self.assertAlmostEqual(profile.cooldown_until, now + 45.0, delta=3.0)
        self.assertEqual(profile.breaker.state, BREAKER_FROZEN)
        self.assertGreaterEqual(profile.breaker.cooldown_until - now, 295.0)
        self.assertTrue(self.auditor.is_host_in_cooldown(host))

    def test_freeze_clamps_ladder_and_adaptive_window(self):
        host = "freeze.example.com"
        profile = self.auditor.get_profile(host)
        profile.verified_ceiling = 4
        self.auditor.note_pressure_failure(host, "task-1", status_code=429)

        allowed, target, because = self.auditor.admission_decision(host, "task-2", active=0)
        self.assertFalse(allowed)
        self.assertEqual(target, 1)
        self.assertIn("frozen", because.lower())

        async def exercise():
            window = AdaptiveHostWindow(8, initial_window=4, host=host, auditor=self.auditor)
            self.assertEqual(window.window, 4)
            await window.warm_start(6)
            self.assertLessEqual(window.window, 1)
            self.assertEqual(window.effective_window(), 1)
            await window.acquire()
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(window.acquire(), 0.2)
            await window.release(False)

        asyncio.run(exercise())

    def test_throttle_release_records_circuit_failure(self):
        async def exercise():
            window = AdaptiveHostWindow(4, host="circuit.example.com", auditor=self.auditor)
            circuit = AdaptiveCircuitBreaker(threshold=1)
            window.circuit = circuit
            await window.release(False, status_code=429)
            with self.assertRaises(CircuitOpenError):
                await circuit.before()

        asyncio.run(exercise())

    def test_probation_promotes_after_k_sustained_successes(self):
        host = "probe.example.com"
        self.auditor.note_pressure_failure(host, "task-1", status_code=429)
        profile = self.auditor.get_profile(host)
        self._expire_cooldown(host)

        allowed, target, _ = self.auditor.admission_decision(host, "probe-task", active=0)
        self.assertTrue(allowed)
        self.assertEqual(target, 1)
        self.assertEqual(profile.breaker.state, BREAKER_PROBATION)
        self.assertIn("BREAKER_PROBING", self.events())

        allowed, target, _ = self.auditor.admission_decision(host, "probe-task-2", active=1)
        self.assertTrue(allowed)
        self.assertEqual(target, 2)
        self.assertEqual(profile.breaker.probe_task_id, "probe-task-2")

        base = time.time()
        for step in range(5):
            with self.auditor._lock:
                self.auditor._note_probe_success_locked(
                    profile, "probe-task-2", bytes_delta=4096, completed_range=False, at=base + step * 4.0
                )

        self.assertEqual(profile.breaker.state, BREAKER_CLOSED)
        self.assertEqual(profile.verified_ceiling, 2)
        self.assertIn("BREAKER_RECOVERED", self.events())

        allowed, target, _ = self.auditor.admission_decision(host, None, active=0)
        self.assertTrue(allowed)
        self.assertIn("BREAKER_CLOSED", self.events())

    def test_probation_promotes_after_three_full_ranges(self):
        host = "range.example.com"
        self.auditor.note_pressure_failure(host, "task-1", status_code=429)
        profile = self.auditor.get_profile(host)
        self._expire_cooldown(host)

        self.auditor.admission_decision(host, "range-task", active=0)
        self.auditor.admission_decision(host, "range-task-2", active=1)

        for _ in range(3):
            self.auditor.record_stream_finished(
                host, "range-task-2", success=True, completed_bytes=1024, expected_bytes=1024
            )

        self.assertEqual(profile.breaker.state, BREAKER_CLOSED)
        self.assertEqual(profile.verified_ceiling, 2)

    def test_probation_failure_refreezes_with_doubled_cooldown(self):
        host = "refreeze.example.com"
        self.auditor.note_pressure_failure(host, "task-1", status_code=429)
        profile = self.auditor.get_profile(host)
        self._expire_cooldown(host)
        self.auditor.admission_decision(host, "probe", active=0)
        self.assertEqual(profile.breaker.state, BREAKER_PROBATION)

        self.auditor.note_pressure_failure(host, "probe", status_code=429)

        self.assertEqual(profile.breaker.state, BREAKER_FROZEN)
        self.assertEqual(profile.breaker.consecutive_trips, 2)
        remaining = profile.breaker.cooldown_until - time.time()
        self.assertGreater(remaining, 550.0)
        self.assertLessEqual(remaining, 601.0)

    def test_sticky_clamp_regression_reopens_after_cooldown(self):
        host = "sticky.example.com"
        self.auditor.record_stream_denied(
            host, "task-x", status_code=429, response_text="rate limit", cooldown_seconds=60.0
        )
        profile = self.auditor.get_profile(host)
        self.assertEqual(profile.verified_ceiling, 1)

        profile.cooldown_until = time.time() - 1.0
        allowed, _, _ = self.auditor.admission_decision(host, "reopen", active=0)
        self.assertTrue(allowed, "host must reopen once the denial cooldown elapses")
        self.auditor.record_stream_started(host, "reopen")

        allowed, target, because = self.auditor.admission_decision(host, "reopen-2", active=1)
        self.assertTrue(allowed, "host must be able to ladder above the clamped ceiling")
        self.assertEqual(target, 2)
        self.assertIn("PROBE_REOPENED", self.events())

    def test_ceiling_decays_after_24h_clean(self):
        host = "decay.example.com"
        profile = self.auditor.get_profile(host)
        profile.verified_ceiling = 2
        profile.ceiling_set_at = time.time() - (25 * 3600.0)

        self.auditor.admission_decision(host, None, active=0)

        self.assertEqual(profile.verified_ceiling, 3)
        self.assertIn("CEILING_DECAYED", self.events())

    def test_frozen_breaker_persists_across_reload(self):
        host = "persist-breaker.example.com"
        from engine.db import TaskStore

        store = TaskStore(self.temp_dir / "breaker_tasks.db")
        try:
            auditor = HostConcurrencyAuditor(
                log_path=self.temp_dir / "audit_persist.jsonl",
                store=store,
                profiles_path=self.temp_dir / "profiles_persist.json",
            )
            auditor.note_pressure_failure(host, "task-1", status_code=429)

            reloaded = HostConcurrencyAuditor(
                log_path=self.temp_dir / "audit_reload.jsonl",
                store=store,
                profiles_path=self.temp_dir / "profiles_reload.json",
            )
            reloaded.load_persisted_profiles()
            profile = reloaded.get_profile(host)
            self.assertEqual(profile.breaker.state, BREAKER_FROZEN)
            self.assertGreaterEqual(profile.breaker.cooldown_until - time.time(), 250.0)
            allowed, target, _ = reloaded.admission_decision(host, None, active=0)
            self.assertFalse(allowed)
            self.assertEqual(target, 1)
        finally:
            store.close()


class ResizableSemaphoreTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def test_waiter_safe_resize_wakes_waiters(self):
        manager = StorageHostConcurrencyManager(self.temp_dir / "storage", default_limit=1)
        host = "tunnel5.dlproxy.uk"
        manager.set_limit(host, 1, calibrated=True)

        async def exercise():
            sem = manager.get_semaphore(host)
            self.assertTrue(hasattr(sem, "resize"))
            await sem.acquire()
            self.assertTrue(sem.locked())

            acquired = asyncio.Event()
            release_gate = asyncio.Event()

            async def waiter():
                async with sem:
                    acquired.set()
                    await release_gate.wait()

            task = asyncio.create_task(waiter())
            await asyncio.sleep(0.05)
            self.assertFalse(acquired.is_set())

            manager.set_limit(host, 2, calibrated=True)
            self.assertIs(manager.get_semaphore(host), sem, "waiter object must not be replaced")
            await asyncio.wait_for(acquired.wait(), 2.0)
            self.assertEqual(sem.in_use, 2)

            release_gate.set()
            await task
            self.assertEqual(sem.in_use, 1)
            await sem.release()
            self.assertEqual(sem.in_use, 0)

        asyncio.run(exercise())

    def test_breaker_clamps_storage_limit(self):
        auditor = HostConcurrencyAuditor(
            log_path=self.temp_dir / "audit_storage.jsonl",
            profiles_path=self.temp_dir / "profiles_storage.json",
        )
        manager = StorageHostConcurrencyManager(self.temp_dir / "storage2", default_limit=4, auditor=auditor)
        host = "datanodes.to"
        manager.set_limit(host, 4, calibrated=True)
        self.assertEqual(manager.get_limit(host), 4)

        auditor.note_pressure_failure(host, "task-1", status_code=429)

        self.assertEqual(manager.get_limit(host), 1)
        sem = manager.get_semaphore(host)
        self.assertEqual(sem.limit, 1)


class SchedulerDedupeTests(unittest.TestCase):
    def test_scheduler_dedupe_rejects_duplicate(self):
        telemetry_bus.clear()

        async def exercise():
            scheduler = FairAsyncScheduler(2)
            await scheduler.start()
            started = asyncio.Event()
            release = asyncio.Event()
            runs = []

            async def job():
                runs.append(1)
                started.set()
                await release.wait()

            first = asyncio.create_task(
                scheduler.submit("archive:pkg", job, dedupe_key="archive:pkg:key")
            )
            await asyncio.wait_for(started.wait(), 2.0)

            await asyncio.wait_for(
                scheduler.submit("archive:pkg", job, dedupe_key="archive:pkg:key"), 1.0
            )
            self.assertEqual(len(runs), 1, "duplicate job must not be queued or run")

            release.set()
            await asyncio.wait_for(first, 2.0)

            await asyncio.wait_for(
                scheduler.submit("archive:pkg", job, dedupe_key="archive:pkg:key"), 2.0
            )
            self.assertEqual(len(runs), 2, "dedupe must release the key after completion")
            await scheduler.close()

        asyncio.run(exercise())

        rejected = [
            event for event in telemetry_bus.query(subsystem="engine:scheduler", limit=50)
            if "SCHEDULER_DEDUPE" in event.get("message", "")
        ]
        self.assertTrue(rejected, "duplicate rejection must be logged to structured telemetry")


if __name__ == "__main__":
    unittest.main()
