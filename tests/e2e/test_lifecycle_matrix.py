"""Deterministic full-lifecycle edge-case matrix.

Each scenario drives the real ``EngineService`` through the production seams and
asserts the resulting lifecycle: timer stage, shared CAPTCHA clearance across
multipart siblings, retry after token rejection, solver timeout, checksum
failure, and archive extraction + source cleanup.

Wave 5 of the multipart master plan adds three fault injections on top of the
happy path: 429/Retry-After backpressure, a package volume that never lands,
and a transient Windows file lock during source cleanup.  Those assertions are
deliberately strict -- a failure here documents a real engine defect and must be
root-caused rather than relaxed.

Artifacts (full telemetry timeline + summary, including the fault evidence) are
written to ``.test-artifacts/lifecycle-matrix/<scenario>/`` for post-mortem
inspection.  Slow variants are gated behind ``TRANSFER_MANAGER_DEEP=1``.

The matrix is intentionally offline and deterministic so it runs inside the
normal quality gate; a live-network failure can never be the reason it is green.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from engine.timer_detector import TimerDetector
from tests.e2e.lifecycle_harness import (
    Scenario,
    ScenarioResult,
    deep_enabled,
    default_scenarios,
    fault_failures,
    fault_scenarios,
    run_scenario,
)

ARTIFACT_ROOT = Path(".test-artifacts") / "lifecycle-matrix"


def _fault_scenario(name: str) -> Scenario:
    for scenario in fault_scenarios():
        if scenario.name == name:
            return scenario
    raise AssertionError(f"unknown fault scenario {name}")


def _run(scenario: Scenario) -> ScenarioResult:
    with tempfile.TemporaryDirectory() as temp:
        return run_scenario(scenario, Path(temp), ARTIFACT_ROOT)


class LifecycleMatrixTests(unittest.TestCase):
    def test_full_lifecycle_matrix(self):
        for scenario in default_scenarios():
            with self.subTest(scenario=scenario.name):
                with tempfile.TemporaryDirectory() as temp:
                    result = run_scenario(scenario, Path(temp), ARTIFACT_ROOT)

                for task_id in result.task_ids:
                    self.assertIn(
                        result.final_states[task_id], scenario.expect_states,
                        f"{scenario.name}: task {task_id[:8]} ended {result.final_states[task_id]}; "
                        f"see {result.artifact_dir}")
                if scenario.expect_challenges is not None:
                    self.assertEqual(
                        result.challenge_count, scenario.expect_challenges,
                        f"{scenario.name}: expected {scenario.expect_challenges} challenge(s), "
                        f"saw {result.challenge_count}; see {result.artifact_dir}")
                if scenario.expect_timer_stage:
                    self.assertTrue(
                        any("hoster_wait_timer" in seq for seq in result.stages.values()),
                        f"{scenario.name}: no hoster_wait_timer stage; stages={result.stages}")
                if scenario.expect_archive_completed:
                    completed = [j for j in result.archive_jobs if j.get("state") == "completed"]
                    self.assertTrue(completed, f"{scenario.name}: no completed archive job; see {result.artifact_dir}")
                    self.assertIn("content.bin", result.destination_files)
                    self.assertFalse(
                        any(name.endswith(".rar") for name in result.destination_files),
                        f"{scenario.name}: source volumes were not cleaned: {result.destination_files}")
                # No unexpected engine errors (a real traceback means a crash).
                self.assertEqual(
                    [e for e in result.errors if "Traceback" in e], [],
                    f"{scenario.name}: engine errors: {result.errors}; see {result.artifact_dir}")


class ThrottleBackpressureTests(unittest.TestCase):
    """429 + Retry-After on live range requests (master plan wave 5, item 1).

    One scenario run feeds both assertions: durable progress must survive the
    throttle, and the breaker must reopen instead of pinning the host at 1.
    """

    result: ScenarioResult
    scenario: Scenario

    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario = _fault_scenario("throttle_429_retry_after")
        cls.result = _run(cls.scenario)

    def test_partial_progress_survives_throttle(self):
        for task_id in self.result.task_ids:
            self.assertEqual(
                self.result.final_states[task_id], "completed",
                f"throttled task did not finish; see {self.result.artifact_dir}")
        throttle = self.result.fault["throttle"]
        self.assertEqual(
            throttle["faults_injected"], self.scenario.throttle_requests,
            f"the 429 injection never fired, so nothing was proven: {throttle}")
        self.assertEqual(
            self.result.fault["part_deletions"], [],
            "the .part file was deleted during a throttle: multi-GB progress would be lost; "
            f"see {self.result.artifact_dir}")
        held = [w for w in self.result.fault["permits_released_during_backoff"] if not w["released"]]
        self.assertEqual(
            held, [], f"permits were held across the Retry-After backoff: {held}")
        self.assertIn(
            "BREAKER_TRIPPED", self.result.fault["breaker_events"],
            f"429/Retry-After did not reach the host pressure breaker: "
            f"{self.result.fault['breaker_events']}")

    def test_breaker_recovers_instead_of_pinning_the_host(self):
        recovery = self.result.fault["breaker_recovery"]
        self.assertEqual(
            recovery["before"]["state"], "frozen",
            "the breaker should be frozen before recovery is attempted")
        for task_id, state in recovery["recovery_task_states"].items():
            self.assertEqual(state, "completed", f"recovery download {task_id[:8]} ended {state}")
        self.assertEqual(
            recovery["after"]["state"], "closed",
            "after the cooldown elapsed and three transfers succeeded the breaker must walk "
            f"FROZEN -> PROBATION -> CLOSED; snapshot={recovery['after']}; "
            f"see {self.result.artifact_dir}")
        self.assertGreater(
            recovery["after"]["verified_ceiling"], 1,
            f"host ceiling stayed pinned at 1 after recovery: {recovery['after']}")


class MissingArchiveVolumeTests(unittest.TestCase):
    """A package volume that never lands (wave 5, item 2).

    Zero source deletions is the assertion that matters most here: silent data
    loss is the worst possible outcome of this failure mode.
    """

    def test_missing_volume_halts_extraction_and_deletes_nothing(self):
        scenario = _fault_scenario("missing_archive_volume")
        result = _run(scenario)
        kept = [name for name in result.destination_files if name.endswith(".rar")]
        self.assertEqual(
            len(kept), scenario.parts - len(scenario.missing_parts),
            f"source volumes were deleted for an incomplete package (silent data loss): {kept}")
        self.assertEqual(
            [j for j in result.archive_jobs if j.get("state") == "completed"], [],
            "an incomplete package must never report a completed extraction")
        self.assertEqual(
            fault_failures(scenario, result), [],
            f"see {result.artifact_dir}")


class CleanupFileLockTests(unittest.TestCase):
    """Transient WinError 32 during source cleanup (wave 5, item 3)."""

    def test_transient_lock_retries_until_the_delete_succeeds(self):
        scenario = _fault_scenario("cleanup_transient_lock")
        result = _run(scenario)
        lock = result.fault["cleanup_lock"]
        self.assertGreater(
            lock["lock_failures_injected"], 0,
            f"no sharing violation was injected, so nothing was proven: {lock}")
        self.assertTrue(
            [j for j in result.archive_jobs if j.get("state") == "completed"],
            f"archive job did not complete after a transient lock: {result.archive_jobs}")
        # The historical bug unlinked once, swallowed the OSError and completed
        # anyway; that regression leaves the volumes on disk and fails here.
        self.assertEqual(
            [name for name in result.destination_files if name.endswith(".rar")], [],
            f"sources survived a transient lock: the delete did not retry; see {result.artifact_dir}")

    @unittest.skipUnless(deep_enabled(), "deep scenario; set TRANSFER_MANAGER_DEEP=1")
    def test_permanent_lock_is_reported_truthfully(self):
        scenario = _fault_scenario("cleanup_permanent_lock")
        result = _run(scenario)
        self.assertEqual(
            [j for j in result.archive_jobs if j.get("state") == "completed"], [],
            "a job whose sources are still locked must not claim success")
        self.assertEqual(
            fault_failures(scenario, result), [], f"see {result.artifact_dir}")


class TerminalThrottleStatusTests(unittest.TestCase):
    """A single transient 403 must not strand a partially downloaded file."""

    @unittest.skipUnless(deep_enabled(), "deep scenario; set TRANSFER_MANAGER_DEEP=1")
    def test_single_403_does_not_kill_the_transfer(self):
        scenario = _fault_scenario("throttle_403_backpressure")
        result = _run(scenario)
        self.assertEqual(
            result.fault["part_deletions"], [],
            "partial progress must survive a 403 throttle")
        for task_id in result.task_ids:
            self.assertEqual(
                result.final_states[task_id], "completed",
                "a one-shot 403 throttle must be retried, not turned into a terminal failure; "
                f"see {result.artifact_dir}")


class TimerDetectorRegressionTests(unittest.TestCase):
    """The DataNodes step-2 countdown must be parsed from real markup shapes."""

    def test_vue_countdown_component(self):
        html = ('<div class="dl"><download-countdown :countdown="10" '
                ':has-countdown="true" message=""></download-countdown></div>')
        result = TimerDetector().detect_from_html(html)
        self.assertTrue(result.has_timer)
        self.assertEqual(result.seconds_remaining, 10)

    def test_js_countdown_variable(self):
        html = "<script>var countdown = 8;</script>"
        result = TimerDetector().detect_from_html(html)
        self.assertTrue(result.has_timer)
        self.assertEqual(result.seconds_remaining, 8)

    def test_data_attribute_countdown(self):
        html = '<span id="dl_countdown" data-timer="15">15</span>'
        result = TimerDetector().detect_from_html(html)
        self.assertTrue(result.has_timer)
        self.assertEqual(result.seconds_remaining, 15)

    def test_dom_script_targets_download_countdown(self):
        script = TimerDetector.extract_dom_script()
        self.assertIn("download-countdown", script)
        self.assertIn(":countdown", script)


class SharedClearanceBenefitTests(unittest.TestCase):
    """A/B proof: one shared CAPTCHA solve must beat per-part solving.

    The two runs are identical (same 4-part package, same solver delay,
    serialized transfers) except for whether reusable host clearance is shared.
    """

    def _run(self, scenario: Scenario):
        with tempfile.TemporaryDirectory() as temp:
            return run_scenario(scenario, Path(temp), ARTIFACT_ROOT)

    def test_shared_clearance_uses_fewer_solves_and_is_faster(self):
        solver_delay = 0.4
        shared = self._run(Scenario(
            name="ab_shared_clearance", parts=4, timer_seconds=1, require_captcha=True,
            drive_ui_captcha=True, share_clearance=True, serialize_host=True,
            solver_delay_seconds=solver_delay,
        ))
        unshared = self._run(Scenario(
            name="ab_per_part_solve", parts=4, timer_seconds=1, require_captcha=True,
            drive_ui_captcha=True, share_clearance=False, serialize_host=True,
            solver_delay_seconds=solver_delay,
        ))

        # Both packages must still finish.
        for result in (shared, unshared):
            for task_id in result.task_ids:
                self.assertEqual(result.final_states[task_id], "completed")

        # One shared challenge/solve vs one per part.
        self.assertEqual(shared.challenge_count, 1, "shared run should need one challenge")
        self.assertEqual(unshared.challenge_count, 4, "per-part run should need four challenges")
        self.assertEqual(shared.solve_count, 1)
        self.assertEqual(unshared.solve_count, 4)
        self.assertLess(shared.solve_count, unshared.solve_count)

        # The saved solver work must translate into wall-clock time.
        self.assertLess(
            shared.elapsed_seconds, unshared.elapsed_seconds,
            f"shared run ({shared.elapsed_seconds:.2f}s) was not faster than "
            f"per-part ({unshared.elapsed_seconds:.2f}s); "
            f"see {shared.artifact_dir} and {unshared.artifact_dir}")

        # Sanity: the shared run saved at least ~3 solver delays.
        expected_saving = solver_delay * (unshared.solve_count - shared.solve_count)
        self.assertGreaterEqual(
            unshared.elapsed_seconds - shared.elapsed_seconds, expected_saving * 0.5,
            "time saving was much smaller than the saved solve time")


if __name__ == "__main__":
    unittest.main()
