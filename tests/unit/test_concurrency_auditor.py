"""Tests for HostConcurrencyAuditor dynamic ladder and persistent audit logging."""

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from engine.concurrency_auditor import HostConcurrencyAuditor


class TestConcurrencyAuditor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.log_file = Path(self.temp_dir.name) / "audit_test.jsonl"
        self.auditor = HostConcurrencyAuditor(log_path=self.log_file)
        self._stores = []

    def create_store(self, path: Path):
        from engine.db import TaskStore
        store = TaskStore(path)
        self._stores.append(store)
        return store

    def tearDown(self):
        for s in self._stores:
            try:
                s.close()
            except Exception:
                pass
        self.temp_dir.cleanup()

    def test_step_up_and_5s_active_rule(self):
        host = "node42.datanodes.to"
        # 1. Admit first stream
        allowed, target, reason = self.auditor.admission_decision(host)
        self.assertTrue(allowed)
        self.assertEqual(target, 1)

        self.auditor.record_stream_started(host, "task-1")
        prof = self.auditor.get_profile(host)
        self.assertEqual(prof.verified_ceiling, 1)

        # 2. Check probe for 2nd stream
        allowed, target, reason = self.auditor.admission_decision(host)
        self.assertTrue(allowed)
        self.assertEqual(target, 2)
        self.auditor.record_stream_started(host, "task-2")

        # 3. Update progress immediately (< 5s duration) -> should NOT bump verified ceiling yet
        self.auditor.update_stream_progress(host, "task-1", 5 * 1024 * 1024, 2 * 1024 * 1024)
        self.auditor.update_stream_progress(host, "task-2", 5 * 1024 * 1024, 2 * 1024 * 1024)
        self.assertEqual(prof.verified_ceiling, 1)  # Still 1 because duration < 5s

        # 4. Simulate > 5s duration
        st1 = prof.streams["task-1"]
        st2 = prof.streams["task-2"]
        st1.started_at -= 6.0
        st2.started_at -= 6.0

        self.auditor.update_stream_progress(host, "task-1", 6 * 1024 * 1024, 2 * 1024 * 1024)
        self.auditor.update_stream_progress(host, "task-2", 6 * 1024 * 1024, 2 * 1024 * 1024)

        # Now verified ceiling MUST be 2
        self.assertEqual(prof.verified_ceiling, 2)

        # 5. Check audit log has PROBE_VERIFIED
        lines = [json.loads(line) for line in self.log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        events = [e["event"] for e in lines]
        self.assertIn("STREAM_STARTED", events)
        self.assertIn("PROBE_ATTEMPT", events)
        self.assertIn("PROBE_VERIFIED", events)

    def test_denial_clamps_ceiling_and_triggers_cooldown(self):
        host = "rapidgator.net"
        self.auditor.record_stream_started(host, "task-A")
        
        # Stream B gets 429
        clamped = self.auditor.record_stream_denied(host, "task-B", status_code=429, response_text="Too many concurrent downloads", cooldown_seconds=60.0)
        self.assertEqual(clamped, 1)

        prof = self.auditor.get_profile(host)
        self.assertEqual(prof.verified_ceiling, 1)
        self.assertEqual(prof.denied_at_ceiling, 2)
        self.assertGreater(prof.cooldown_until, time.monotonic())

        # Further probes should be denied while cooldown is active
        allowed, target, reason = self.auditor.admission_decision(host)
        self.assertFalse(allowed)
        self.assertIn("cooldown", reason.lower())

    def test_stream_preemption_detected(self):
        host = "uploadgig.com"
        self.auditor.record_stream_started(host, "task-orig")
        prof = self.auditor.get_profile(host)
        st_orig = prof.streams["task-orig"]

        # task-orig has been silent long enough that no ordinary bandwidth
        # sharing explains it.
        st_orig.last_active_at -= 90.0

        # task-new starts
        self.auditor.record_stream_started(host, "task-new")
        st_new = prof.streams["task-new"]
        st_new.started_at -= 4.0

        preempted = self.auditor.detect_preemption(host, "task-new")
        self.assertTrue(preempted)
        self.assertEqual(prof.verified_ceiling, 1)
        self.assertEqual(prof.streams["task-orig"].status, "preempted")

    def test_brief_idle_while_a_sibling_saturates_is_not_preemption(self):
        """The regression that pinned DataNodes to one stream, permanently.

        A stream going quiet for a few seconds while a sibling saturates a
        ~90 MB/s link is ordinary sharing. The old 6s threshold called that
        "the host killed my stream", clamped the ceiling to 1 and recorded a
        denial boundary that never expired, so one unlucky run taught the
        system that the host allows a single stream forever.
        """
        host = "node42.datanodes.to"
        self.auditor.record_stream_started(host, "task-orig")
        prof = self.auditor.get_profile(host)
        prof.verified_ceiling = 3
        prof.streams["task-orig"].last_active_at -= 8.0

        self.auditor.record_stream_started(host, "task-new")
        prof.streams["task-new"].started_at -= 4.0

        self.assertFalse(
            self.auditor.detect_preemption(host, "task-new"),
            "ordinary bandwidth sharing was misread as host preemption")
        self.assertEqual(prof.verified_ceiling, 3, "ceiling was clamped by a false positive")
        self.assertIsNone(prof.denied_at_ceiling, "a false positive poisoned the denial boundary")
        self.assertEqual(prof.streams["task-orig"].status, "active")

    def test_optimistic_ladder_reaches_global_limit_before_denial(self):
        host = "fastnode.example.com"
        # Stream 1 admitted (active=1, verified=1)
        allowed, target, reason = self.auditor.admission_decision(host)
        self.assertTrue(allowed)
        self.assertEqual(target, 1)
        self.auditor.record_stream_started(host, "task-1")

        # Stream 2 is admitted immediately as the first probe.
        allowed, target, reason = self.auditor.admission_decision(host)
        self.assertTrue(allowed)
        self.assertEqual(target, 2)
        self.assertIn("Probing", reason)
        self.auditor.record_stream_started(host, "task-2")

        # The next queued task is allowed immediately; the global policy is
        # the safety bound, not an artificial two-stream ceiling.
        allowed, target, reason = self.auditor.admission_decision(host)
        self.assertTrue(allowed)
        self.assertEqual(target, 3)
        self.assertIn("Probing", reason)
        self.auditor.record_stream_started(host, "task-3")

        allowed, target, reason = self.auditor.admission_decision(host, max_global=4)
        self.assertTrue(allowed)
        self.assertEqual(target, 4)
        self.auditor.record_stream_started(host, "task-4")

        # The configured global maximum stops the ladder, even before a
        # denial is observed.
        allowed, target, reason = self.auditor.admission_decision(host, max_global=4)
        self.assertFalse(allowed)
        self.assertIn("maximum", reason.lower())

        # The persisted verified ceiling may still be one until byte-level
        # validation runs; that does not block the optimistic probe ladder.
        self.assertEqual(self.auditor.get_profile(host).verified_ceiling, 1)

    def test_denial_persists_last_safe_count_after_optimistic_probe(self):
        host = "ladder-denial.example.com"
        self.auditor.record_stream_started(host, "task-1")
        for task_id, expected in (("task-2", 2), ("task-3", 3), ("task-4", 4)):
            allowed, target, _ = self.auditor.admission_decision(host, max_global=4, task_id=task_id)
            self.assertTrue(allowed)
            self.assertEqual(target, expected)
            self.auditor.record_stream_started(host, task_id)

        # The fourth stream is the failed attempt. Its recorded probe level,
        # rather than a race-dependent active-stream count, is used to persist
        # the exact last safe ceiling of three.
        clamped = self.auditor.record_stream_denied(
            host, "task-4", status_code=429,
            response_text="too many concurrent downloads", cooldown_seconds=60.0,
        )
        self.assertEqual(clamped, 3)
        prof = self.auditor.get_profile(host)
        self.assertEqual(prof.verified_ceiling, 3)
        self.assertEqual(prof.denied_at_ceiling, 4)
        self.assertTrue(self.auditor.is_host_in_cooldown(host))

    def test_profile_disk_persistence_and_reload(self):
        db_path = Path(self.temp_dir.name) / "test_tasks.db"
        store = self.create_store(db_path)

        # Auditor A records ceiling 2 and a denial on host X with a 120s cooldown
        auditor_a = HostConcurrencyAuditor(
            log_path=Path(self.temp_dir.name) / "audit_a.jsonl",
            store=store,
            profiles_path=Path(self.temp_dir.name) / "profiles_a.json",
        )
        host = "host-x.example.com"
        auditor_a.record_stream_started(host, "task-1")
        auditor_a.record_stream_started(host, "task-2")
        prof_a = auditor_a.get_profile(host)

        # Qualify both streams to bump verified ceiling to 2
        prof_a.streams["task-1"].started_at -= 6.0
        prof_a.streams["task-2"].started_at -= 6.0
        auditor_a.update_stream_progress(host, "task-1", 2 * 1024 * 1024, 1024 * 1024)
        auditor_a.update_stream_progress(host, "task-2", 2 * 1024 * 1024, 1024 * 1024)
        self.assertEqual(prof_a.verified_ceiling, 2)

        # Denial on stream 3 attempts concurrency 3 -> clamped to 2, denied_at_ceiling=3, cooldown=120s
        clamped = auditor_a.record_stream_denied(
            host, "task-3", status_code=429, response_text="Rate limit exceeded", cooldown_seconds=120.0
        )
        self.assertEqual(clamped, 2)
        self.assertEqual(prof_a.verified_ceiling, 2)
        self.assertEqual(prof_a.denied_at_ceiling, 3)
        self.assertTrue(auditor_a.is_host_in_cooldown(host))

        # Auditor B created pointing to the same DB or JSON path, runs load_persisted_profiles()
        auditor_b = HostConcurrencyAuditor(
            log_path=Path(self.temp_dir.name) / "audit_b.jsonl",
            store=store,
            profiles_path=Path(self.temp_dir.name) / "profiles_b.json",
        )
        auditor_b.load_persisted_profiles()

        prof_b = auditor_b.get_profile(host)
        self.assertEqual(prof_b.verified_ceiling, 2)
        self.assertEqual(prof_b.denied_at_ceiling, 3)
        self.assertTrue(auditor_b.is_host_in_cooldown(host))
        in_cool, remain, reason = auditor_b.is_host_in_cooldown(host)
        self.assertTrue(in_cool)
        self.assertGreater(remain, 100.0)

    def test_expired_cooldown_reloads_as_inactive(self):
        db_path = Path(self.temp_dir.name) / "test_tasks_exp.db"
        store = self.create_store(db_path)
        host = "expired.example.com"

        # Create a profile with cooldown_until = time.time() - 30.0
        past_cooldown = time.time() - 30.0
        store.save_host_concurrency_profile(
            host=host,
            verified_ceiling=2,
            denied_at_ceiling=3,
            cooldown_until=past_cooldown,
            last_probe_at=time.time() - 60.0,
            rejection_reasons=["429 Rate limited"],
        )

        # Reload in auditor, verify is_host_in_cooldown returns False
        auditor = HostConcurrencyAuditor(
            log_path=Path(self.temp_dir.name) / "audit_exp.jsonl",
            store=store,
        )
        auditor.load_persisted_profiles()

        prof = auditor.get_profile(host)
        self.assertEqual(prof.verified_ceiling, 2)
        self.assertEqual(prof.denied_at_ceiling, 3)
        self.assertFalse(auditor.is_host_in_cooldown(host))
        in_cool, remain, reason = auditor.is_host_in_cooldown(host)
        self.assertFalse(in_cool)
        self.assertEqual(remain, 0.0)

        # Expiry must reopen calibration.  A stale denied_at_ceiling=3 must
        # not permanently block the next two-stream probe.
        allowed, target, reason = auditor.admission_decision(host, max_global=4, task_id="probe-after-cooldown")
        self.assertTrue(allowed)
        self.assertEqual(target, 1)
        self.assertIsNone(prof.denied_at_ceiling)
        events = [json.loads(line) for line in Path(self.temp_dir.name, "audit_exp.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertIn("PROBE_REOPENED", [event["event"] for event in events])

    def test_json_fallback_persistence_and_reload(self):
        json_path = Path(self.temp_dir.name) / "fallback_profiles.json"
        auditor_a = HostConcurrencyAuditor(
            log_path=Path(self.temp_dir.name) / "audit_json_a.jsonl",
            profiles_path=json_path,
        )
        host = "json-node.example.com"
        auditor_a.record_stream_started(host, "task-j1")
        auditor_a.record_stream_started(host, "task-j2")
        prof_a = auditor_a.get_profile(host)
        prof_a.streams["task-j1"].started_at -= 6.0
        prof_a.streams["task-j2"].started_at -= 6.0
        auditor_a.update_stream_progress(host, "task-j1", 3 * 1024 * 1024, 1024 * 1024)
        auditor_a.update_stream_progress(host, "task-j2", 3 * 1024 * 1024, 1024 * 1024)
        self.assertEqual(prof_a.verified_ceiling, 2)

        # Auditor B without SQLite store, loading from json_path
        auditor_b = HostConcurrencyAuditor(
            log_path=Path(self.temp_dir.name) / "audit_json_b.jsonl",
            profiles_path=json_path,
        )
        auditor_b.load_persisted_profiles()
        prof_b = auditor_b.get_profile(host)
        self.assertEqual(prof_b.verified_ceiling, 2)

    def test_task_store_host_concurrency_profile_crud(self):
        db_path = Path(self.temp_dir.name) / "test_store_crud.db"
        store = self.create_store(db_path)
        host = "crud.example.com"

        # Initially non-existent
        res = store.get_host_concurrency_profile(host)
        self.assertIsNone(res)

        # Save profile
        now = time.time()
        store.save_host_concurrency_profile(
            host=host,
            verified_ceiling=4,
            denied_at_ceiling=5,
            cooldown_until=now + 300.0,
            last_probe_at=now,
            rejection_reasons=["429 Too Many Requests"],
        )

        res = store.get_host_concurrency_profile(host)
        self.assertIsNotNone(res)
        self.assertEqual(res["host"], host)
        self.assertEqual(res["verified_ceiling"], 4)
        self.assertEqual(res["denied_at_ceiling"], 5)
        self.assertAlmostEqual(res["cooldown_until"], now + 300.0, places=1)
        self.assertEqual(res["rejection_reasons"], ["429 Too Many Requests"])

        # Update via upsert
        store.save_host_concurrency_profile(
            host=host,
            verified_ceiling=3,
            denied_at_ceiling=4,
            cooldown_until=0.0,
            last_probe_at=now + 10.0,
            rejection_reasons=["429 Too Many Requests", "503 Service Unavailable"],
        )
        res2 = store.get_host_concurrency_profile(host)
        self.assertEqual(res2["verified_ceiling"], 3)
        self.assertEqual(res2["denied_at_ceiling"], 4)
        self.assertEqual(len(res2["rejection_reasons"]), 2)

        # List all
        all_profiles = store.list_host_concurrency_profiles()
        self.assertEqual(len(all_profiles), 1)
        self.assertEqual(all_profiles[0]["host"], host)

    def test_speed_dip_is_diagnostic_not_a_clamp(self):
        auditor = HostConcurrencyAuditor(log_path=Path(self.temp_dir.name) / "audit_speed_dip.jsonl")
        host = "rapidgator.net"
        now = time.time()
        auditor.record_stream_started(host, "task-1")
        auditor.record_stream_started(host, "task-2")

        prof = auditor.get_profile(host)
        prof.verified_ceiling = 1
        active = [s for s in prof.streams.values() if s.status == "active"]
        self.assertEqual(len(active), 2)

        auditor.record_speed_dip(host, "task-2", current_speed_bps=500_000, prev_speed_bps=2_000_000)

        # Speed dips are observational only: they must not ratchet the ceiling,
        # set a denial boundary, or impose a cooldown. Denials (HTTP 429/403)
        # are the authoritative clamp signal.
        self.assertEqual(prof.verified_ceiling, 1)
        self.assertIsNone(prof.denied_at_ceiling)
        self.assertEqual(prof.cooldown_until, 0.0)
        self.assertFalse(auditor.is_host_in_cooldown(host)[0])

        with open(auditor.log_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        event_types = [entry["event"] for entry in lines]
        self.assertNotIn("PROBE_CLAMPED_SPEED_DROP", event_types)
        self.assertIn("SPEED_DIP_OBSERVED", event_types)

    def test_response_header_retry_after_cooldown(self):
        auditor = HostConcurrencyAuditor(log_path=Path(self.temp_dir.name) / "audit_retry_after.jsonl")
        host = "files.catbox.moe"
        now = time.time()

        auditor.record_response_headers(host, "task-1", {"Retry-After": "45"})
        prof = auditor.get_profile(host)
        self.assertAlmostEqual(prof.cooldown_until, now + 45.0, delta=2.0)
        in_cd, remain, reason = auditor.is_host_in_cooldown(host)
        self.assertTrue(in_cd)
        self.assertGreater(remain, 40.0)

    def test_response_header_ratelimit_reset(self):
        auditor = HostConcurrencyAuditor(log_path=Path(self.temp_dir.name) / "audit_ratelimit_reset.jsonl")
        host = "api.gofile.io"
        now = time.time()
        reset_ts = now + 90.0

        auditor.record_response_headers(host, "task-1", {"X-RateLimit-Reset": str(int(reset_ts))})
        prof = auditor.get_profile(host)
        self.assertGreaterEqual(prof.cooldown_until, reset_ts - 1.0)
        self.assertTrue(auditor.is_host_in_cooldown(host)[0])

    def test_queue_pump_backs_off_during_host_cooldown(self):
        auditor = HostConcurrencyAuditor(log_path=Path(self.temp_dir.name) / "audit_pump_backoff.jsonl")
        cooling_host = "cooling.example.com"
        active_host = "active.example.com"

        # Impose cooldown on cooling_host
        auditor.record_stream_denied(cooling_host, "task-c1", status_code=429, cooldown_seconds=60.0)

        self.assertTrue(auditor.is_host_in_cooldown(cooling_host)[0])
        self.assertFalse(auditor.is_host_in_cooldown(active_host)[0])

        # Simulated pump dispatch evaluation:
        tasks = [
            {"id": "t-1", "source_url": "https://cooling.example.com/file1.zip"},
            {"id": "t-2", "source_url": "https://active.example.com/file2.zip"},
        ]
        dispatched = []
        for t in tasks:
            h = (urlsplit(t["source_url"]).hostname or "").lower()
            in_cd, remain, reason = auditor.is_host_in_cooldown(h)
            if in_cd:
                continue
            dispatched.append(t["id"])

        self.assertEqual(dispatched, ["t-2"])

    def test_service_boot_profile_synchronization(self):
        db_path = Path(self.temp_dir.name) / "test_service_boot.db"
        store = self.create_store(db_path)
        host = "boot.example.com"

        # Pre-seed persistent profile
        store.save_host_concurrency_profile(
            host=host,
            verified_ceiling=3,
            denied_at_ceiling=4,
            cooldown_until=0.0,
            last_probe_at=time.time(),
            rejection_reasons=["429 Too Many Requests"],
        )

        # Fresh auditor simulates service boot
        fresh_auditor = HostConcurrencyAuditor(
            log_path=Path(self.temp_dir.name) / "audit_boot.jsonl",
            store=store,
        )
        fresh_auditor.load_persisted_profiles()

        prof = fresh_auditor.get_profile(host)
        self.assertEqual(prof.verified_ceiling, 3)
        self.assertEqual(prof.denied_at_ceiling, 4)
        self.assertFalse(fresh_auditor.is_host_in_cooldown(host)[0])

    def test_speed_dip_is_observational(self):
        """Speed dips record telemetry but do not lock the ceiling or suppress probes."""
        host = "node42.datanodes.to"
        self.auditor.record_stream_started(host, "task-1")
        self.auditor.record_stream_started(host, "task-2")
        prof = self.auditor.get_profile(host)
        prof.verified_ceiling = 3

        self.auditor.record_speed_dip(
            host, "task-1",
            current_speed_bps=20_000,
            prev_speed_bps=79_000_000,
            reason="Observed dip",
        )
        self.assertFalse(prof.ceiling_locked)
        self.assertEqual(prof.verified_ceiling, 3)

    def test_no_api_demotes_a_host_for_being_slow(self):
        """`record_starvation_incident` is gone, and must not come back.

        It demoted a host's ceiling to the count of streams moving bytes and
        locked it there, on the theory that a slow stream proved the host would
        only serve that many. It does not: DataNodes assigns a lane per session
        and some lanes are simply slow, so this fired constantly and pinned
        multipart packages to one stream at a time. Genuine refusal arrives as
        429/403 and belongs to the breaker.
        """
        self.assertFalse(
            hasattr(self.auditor, "record_starvation_incident"),
            "a slow stream can demote and lock a host's ceiling again")


if __name__ == "__main__":
    unittest.main()
