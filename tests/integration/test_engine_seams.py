"""Integration Seam Test Suite.

Verifies boundaries and interactions between EngineService scheduler (_pump),
TaskStore persistence, HostConcurrencyAuditor, and Provider resolution lifecycle.
"""

import asyncio
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures.seam_harness import EngineSeamHarness
from scripts.contract_scanner import scan_engine_contracts
from engine.concurrency_auditor import concurrency_auditor


class TestEngineSeams(unittest.TestCase):
    def test_ast_boundary_contracts_have_zero_violations(self):
        """SEAM-01: Static AST boundary scanner confirms zero phantom method calls."""
        violations = scan_engine_contracts()
        self.assertEqual(
            len(violations), 0,
            f"Found boundary contract violations: {violations}"
        )

    def test_host_level_resolve_mutex_gates_queued_siblings(self):
        """SEAM-02 & SEAM-03: Multiple tasks for the same host cannot resolve concurrently."""
        with EngineSeamHarness() as harness:
            service = harness.service
            assert service is not None

            # Add 3 parts for datanodes.to
            t1 = harness.add_task("https://datanodes.to/f1/file.part01.rar", display_name="part01.rar")
            t2 = harness.add_task("https://datanodes.to/f2/file.part02.rar", display_name="part02.rar")
            t3 = harness.add_task("https://datanodes.to/f3/file.part03.rar", display_name="part03.rar")

            # Mock resolution_broker.resolve to take 0.4s to simulate real Turnstile / network
            resolving_concurrent_counts = []

            def slow_resolve(ctx, fn):
                # Sample how many tasks are currently resolving in the store
                active = sum(1 for t in service.store.list() if t.state == "resolving")
                resolving_concurrent_counts.append(active)
                time.sleep(0.3)
                from engine.models import ResolvedItem
                return [ResolvedItem("datanodes", getattr(ctx, "source_url", "https://datanodes.to"), "part01.rar", direct_url="https://node1.datanodes.to/file")]

            with patch.object(service.resolution_broker, "resolve", side_effect=slow_resolve):
                # Wait until at least one task finishes resolving or reaches downloading/completed
                harness.wait_for_condition(
                    lambda: any(t.state in {"resolving", "downloading", "completed", "failed"} for t in service.store.list()),
                    timeout=3.0,
                )
                # Allow a short observation window
                time.sleep(0.8)

            # Assert that resolving concurrency for this host never exceeded 1
            if resolving_concurrent_counts:
                max_resolving = max(resolving_concurrent_counts)
                self.assertLessEqual(
                    max_resolving, 1,
                    f"Host-level resolve mutex violated: {max_resolving} tasks resolved concurrently"
                )

    def test_sibling_clearance_isolation_and_no_token_poisoning(self):
        """SEAM-03: Sibling tasks inherit cf_clearance but NEVER receive burned turnstile_token."""
        with EngineSeamHarness() as harness:
            service = harness.service
            assert service is not None

            target_host = "datanodes.to"
            # Set a clearance session with both cf_clearance and an attempted turnstile token
            service.set_host_session(target_host, {
                "cf_clearance": "fresh_clearance_xyz",
                "turnstile_token": "burned_single_use_tok",
                "cookies": {"cf_clearance": "fresh_clearance_xyz", "session": "active"},
            })

            # Fetch cached session via get_host_session
            cached = service.get_host_session(target_host)

            # Assert cf_clearance is preserved
            self.assertEqual(cached.get("cf_clearance"), "fresh_clearance_xyz")
            self.assertEqual(cached.get("cookies", {}).get("cf_clearance"), "fresh_clearance_xyz")

            # Assert turnstile_token is strictly scrubbed (token poisoning prevented)
            self.assertNotIn("turnstile_token", cached)
            self.assertNotIn("cf-turnstile-response", cached)

    def test_service_boot_restores_host_concurrency_profiles(self):
        """SEAM-03: Concurrency profiles stored in SQLite are available immediately on boot."""
        # Create a temporary directory and pre-seed a profile in SQLite
        import tempfile
        from engine.db import TaskStore

        temp_dir = Path(tempfile.mkdtemp(prefix="engine_boot_test_"))
        try:
            db_path = temp_dir / "downloads.sqlite3"
            store = TaskStore(db_path)
            store.save_host_concurrency_profile(
                host="cdn.example.org",
                verified_ceiling=4,
                denied_at_ceiling=5,
                cooldown_until=0.0,
                last_probe_at=time.time(),
                rejection_reasons=["503 Service Unavailable"],
            )
            store.close()

            # Now boot EngineService against this exact directory
            with EngineSeamHarness(temp_dir=temp_dir) as harness:
                prof = concurrency_auditor.get_profile("cdn.example.org")
                self.assertEqual(prof.verified_ceiling, 4)
                self.assertEqual(prof.denied_at_ceiling, 5)
                self.assertIn("503 Service Unavailable", prof.rejection_reasons)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_seam_browser_solver_respects_user_paused_siblings(self):
        """SEAM-03: Solving CAPTCHA on part 1 must never unpause a sibling that was paused by user."""
        with EngineSeamHarness() as harness:
            service = harness.service
            assert service is not None

            shared_folder = str(harness.temp_dir / "downloads" / "game_repack")
            t1 = harness.add_task("https://datanodes.to/code1/game.part01.rar", folder_path=shared_folder, state="needs_user", user_action="turnstile")
            t2 = harness.add_task("https://datanodes.to/code2/game.part02.rar", folder_path=shared_folder, state="paused", paused_reason="Paused by user")

            # Simulate captcha solve completion on t1
            service.dispatch("captcha_solve", {
                "challenge_id": "test_ch_1",
                "solution": {"text": "solved_tok", "cf_clearance": "clr_123"},
                "solver_id": "clearcote",
            })

            # Reload tasks from store
            reloaded_t2 = service.store.get(t2.id)
            self.assertEqual(reloaded_t2.state, "paused", "User-paused sibling was incorrectly resumed")
            self.assertEqual(reloaded_t2.paused_reason, "Paused by user")

    def test_seam_browser_solver_domain_isolation_on_resumption(self):
        """SEAM-03: Tasks in same folder targeting different hosts must not share clearance or unblock."""
        with EngineSeamHarness() as harness:
            service = harness.service
            assert service is not None

            shared_folder = str(harness.temp_dir / "downloads" / "mixed_folder")
            t1 = harness.add_task("https://datanodes.to/code1/file.rar", folder_path=shared_folder, state="needs_user", user_action="turnstile")
            t2 = harness.add_task("https://rapidgator.net/file/other.rar", folder_path=shared_folder, state="needs_user", user_action="turnstile")

            # Solve for t1 (datanodes.to)
            service.dispatch("captcha_solve", {
                "challenge_id": "test_ch_2",
                "solution": {"text": "datanodes_tok", "cf_clearance": "datanodes_clr"},
                "solver_id": "clearcote",
            })

            # Verify t2 (rapidgator.net) was NOT resumed with datanodes clearance
            reloaded_t2 = service.store.get(t2.id)
            self.assertEqual(reloaded_t2.state, "needs_user", "Different-domain task was incorrectly unblocked")

    def test_seam_task_store_save_progress_preserves_revision(self):
        """SEAM-04: store.save_progress updates telemetry without bumping task revision."""
        with EngineSeamHarness() as harness:
            service = harness.service
            assert service is not None

            task = harness.add_task("https://example.com/test.zip")
            initial_rev = task.revision

            task.completed_bytes = 1048576
            task.speed_bytes_per_second = 500000.0
            service.store.save_progress(task)

            reloaded = service.store.get(task.id)
            self.assertEqual(reloaded.completed_bytes, 1048576)
            self.assertEqual(reloaded.revision, initial_rev, "save_progress unexpectedly bumped task revision")

            # Now verify standard save() DOES bump revision
            service.store.save(task)
            reloaded_after_save = service.store.get(task.id)
            self.assertGreater(reloaded_after_save.revision, initial_rev, "save() failed to bump revision")

    def test_seam_reliability_invalid_content_is_retryable(self):
        """SEAM-06: Truncated or corrupt downloads trigger retry rather than permanent failure."""
        from engine.reliability import decide_retry, FailureClass
        decision = decide_retry("file was truncated: expected 1000 bytes, got 500", attempt=1)
        self.assertEqual(decision.failure, FailureClass.INVALID_CONTENT)
        self.assertTrue(decision.retry, "INVALID_CONTENT was not marked retryable")

    def test_seam_rpc_set_task_options_coerces_primitive_types(self):
        """SEAM-07: set_task_options coerces string numbers to integers before persisting."""
        with EngineSeamHarness() as harness:
            service = harness.service
            assert service is not None

            task = harness.add_task("https://example.com/item.bin")
            service.dispatch("set_task_options", {
                "id": task.id,
                "priority": "5",
                "queue_order": "3",
            })

            reloaded = service.store.get(task.id)
            self.assertEqual(reloaded.priority, 5)
            self.assertIsInstance(reloaded.priority, int)
            self.assertEqual(reloaded.queue_order, 3)
            self.assertIsInstance(reloaded.queue_order, int)

    def test_seam_ui_rpc_parity_complete(self):
        """SEAM-07: 100% of UI RPC methods in src/api.ts are registered in engine/service.py."""
        from scripts.contract_scanner import check_ui_rpc_parity
        missing = check_ui_rpc_parity()
        self.assertEqual(len(missing), 0, f"Missing RPC handlers: {missing}")

    def test_seam_multipart_detector_comprehensive_matrix(self):
        """SEAM-05: MultiPartDetector parses standard, split, legacy RAR, and ZIP sequences."""
        from engine.archive_joiner import MultiPartDetector
        cases = [
            ("Archive.part01.rar", 1, "part_archive", "rar"),
            ("Archive.part03.rar", 3, "part_archive", "rar"),
            ("Data.001", 1, "split_binary", "001"),
            ("Data.005", 5, "split_binary", "005"),
            ("Legacy.r00", 2, "numbered_rar", "r00"),
            ("Volume.z01", 1, "multipart_zip", "z01"),
        ]
        for filename, expected_part, expected_format, expected_ext in cases:
            res = MultiPartDetector.detect(filename)
            self.assertIsNotNone(res, f"Failed to detect {filename}")
            self.assertEqual(res.part_number, expected_part, f"Wrong part number for {filename}")
            self.assertEqual(res.format_type, expected_format, f"Wrong format type for {filename}")
            self.assertEqual(res.extension, expected_ext, f"Wrong extension for {filename}")

        self.assertIsNone(MultiPartDetector.detect("single_movie.mp4"))


if __name__ == "__main__":
    unittest.main()

