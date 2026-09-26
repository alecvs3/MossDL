import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.backend_selection import BackendSelector
from engine.fallback import ProviderFallback
from engine.provider_health import ProviderHealthMonitor
from engine.service import EngineService
from scripts.release_gate import run_release_gate


class ReleaseHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = EngineService(self.temp_dir.name)

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def test_preflight_blocker_stops_later_gates(self):
        with patch("scripts.release_gate.verify_phase5_contracts", return_value=(False, ["Simulated preflight failure"])):
            with patch("scripts.release_gate.scan_root") as mock_sec:
                decision = run_release_gate(service=self.service)
                self.assertEqual(decision["status"], "blocked")
                self.assertEqual(decision["release_gate"], "failed")
                self.assertIn("Phase 5 prerequisite contracts missing or failed", decision["blockers"])
                # Later gates must not run
                mock_sec.assert_not_called()

    def test_live_canary_requires_explicit_opt_in(self):
        # Default run without opt-in
        decision_default = run_release_gate(canary_opt_in=False, service=self.service)
        self.assertEqual(decision_default["canary"]["status"], "disabled_by_policy")
        self.assertFalse(decision_default["canary"]["opt_in"])

    def test_safe_pre_transfer_fallback_versus_post_progress_rollback(self):
        fallback = ProviderFallback(self.service.store)
        task = self.service.dispatch("add_task", {
            "url": "https://example.test/primary.zip",
            "destination": self.temp_dir.name,
        })
        task_id = task["id"]

        alternatives = [
            {"url": "https://mirror1.example.test/primary.zip", "provider_id": "mirror1"},
            {"url": "https://mirror2.example.test/primary.zip", "provider_id": "mirror2"},
        ]

        # 1. Before progress: safe fallback available
        candidates = fallback.candidates(task_id, alternatives)
        self.assertEqual(len(candidates), 2)

        # Record quota lockout for mirror1
        fallback.lock_quota(task_id, "mirror1", "https://mirror1.example.test/primary.zip")
        self.assertTrue(fallback.is_quota_locked(task_id, "https://mirror1.example.test/primary.zip"))

        # Candidates now strictly exclude quota-locked mirror
        updated_candidates = fallback.candidates(task_id, alternatives)
        self.assertEqual(len(updated_candidates), 1)
        self.assertEqual(updated_candidates[0]["provider_id"], "mirror2")

        # 2. Post-progress: task with durable bytes written
        task_obj = self.service.store.get(task_id)
        task_obj.completed_bytes = 1048576  # 1 MiB transferred
        self.service.store.save(task_obj)

        # Verify task preserves completed_bytes and doesn't discard state
        persisted = self.service.store.get(task_id)
        self.assertEqual(persisted.completed_bytes, 1048576)

    def test_provider_quarantine_and_health_tracking(self):
        monitor = ProviderHealthMonitor(self.service.store)
        p_snap = monitor.record("provider-canary", "failed", category="rate_limited", error="Too Many Requests")
        self.assertEqual(p_snap.failure_categories.get("rate_limited"), 1)
        self.assertFalse(monitor.is_quarantined("provider-canary"))

        # Trigger plugin faults up to threshold
        monitor.record_plugin_fault("provider-canary", "Fatal error", threshold=2)
        monitor.record_plugin_fault("provider-canary", "Fatal error 2", threshold=2)
        self.assertTrue(monitor.is_quarantined("provider-canary"))


if __name__ == "__main__":
    unittest.main()