"""Unit tests for the deterministic lifecycle stage machine (Phase 11).

Covers the legal-transition table, microsecond transition telemetry,
solver/coordinator/archive stage mapping, detail redaction, and DB roundtrip.
"""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from engine import lifecycle
from engine.db import TaskStore
from engine.models import ArchiveJob, DownloadTask
from engine.service import EngineService


def make_service() -> EngineService:
    service = EngineService.__new__(EngineService)
    service.store = MagicMock()
    service.events = MagicMock()
    service._log_task = MagicMock()
    return service


def make_task(state: str = "resolving", stage: str | None = None) -> DownloadTask:
    return DownloadTask(
        source_url="https://datanodes.to/abc/Game.part1.rar",
        destination="N:\\dl",
        display_name="Game.part1.rar",
        state=state,
        stage=stage,
    )


class TestStageTransitionTable(unittest.TestCase):
    """The 12-stage deterministic machine: legality and labels."""

    def test_full_happy_path_is_legal(self):
        path = [
            "resolving_metadata", "hoster_wait_timer", "captcha_challenge_detected",
            "captcha_solving", "captcha_verifying", "direct_link_acquired",
            "downloading", "verifying_integrity", "unraring_pending",
            "unraring_extracting", "archive_cleanup", "completed",
        ]
        for index in range(len(path) - 1):
            self.assertTrue(
                lifecycle.is_legal_transition(path[index], path[index + 1]),
                f"{path[index]} -> {path[index + 1]} must be legal")

    def test_retry_reentry_from_failed(self):
        self.assertTrue(lifecycle.is_legal_transition("failed", "resolving_metadata"))
        self.assertTrue(lifecycle.is_legal_transition("failed", "downloading"))

    def test_post_completion_archive_pipeline_is_legal(self):
        self.assertTrue(lifecycle.is_legal_transition("completed", "unraring_pending"))
        self.assertTrue(lifecycle.is_legal_transition("verifying_integrity", "archive_cleanup"))
        self.assertTrue(lifecycle.is_legal_transition("archive_cleanup", "completed"))

    def test_illegal_transitions_rejected(self):
        self.assertFalse(lifecycle.is_legal_transition("completed", "downloading"))
        self.assertFalse(lifecycle.is_legal_transition("archive_cleanup", "downloading"))
        self.assertFalse(lifecycle.is_legal_transition("direct_link_acquired", "resolving_metadata"))

    def test_none_and_reentry_are_legal(self):
        self.assertTrue(lifecycle.is_legal_transition(None, "downloading"))
        self.assertTrue(lifecycle.is_legal_transition("downloading", "downloading"))

    def test_every_stage_has_label(self):
        for stage in lifecycle.STAGE_TRANSITIONS:
            self.assertTrue(lifecycle.stage_label(stage))


class TestTransitionStage(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_stage_change_persists_event_with_microsecond_timestamp(self):
        task = make_task(state="resolving", stage="resolving_metadata")
        self.service._transition_stage(task, "hoster_wait_timer", {"countdown_seconds": 28},
                                        source="solver:countdown")
        self.assertEqual(task.stage, "hoster_wait_timer")
        self.assertIsNotNone(task.stage_entered_at)
        self.assertEqual(len(task.stage_history), 1)
        entry = task.stage_history[0]
        self.assertEqual(entry["from"], "resolving_metadata")
        self.assertEqual(entry["to"], "hoster_wait_timer")
        self.assertFalse(entry["forced"])
        self.assertIn(".", entry["entered_at_us"])
        # Durable TaskStageChanged event with the canonical payload.
        save_args = self.service.store.save_with_event.call_args
        self.assertEqual(save_args[0][1], "TaskStageChanged")
        payload = save_args[0][2]
        self.assertEqual(payload["stage"], "hoster_wait_timer")
        self.assertEqual(payload["stage_detail"]["countdown_seconds"], 28)
        self.assertEqual(payload["source"], "solver:countdown")

    def test_same_stage_refresh_avoids_revision_churn(self):
        task = make_task(state="resolving", stage="hoster_wait_timer")
        task.stage_entered_at = lifecycle.now_us() - 5.0
        self.service._transition_stage(task, "hoster_wait_timer", {"countdown_seconds": 12},
                                        source="solver:countdown", refresh_only=True)
        self.assertEqual(task.stage, "hoster_wait_timer")
        self.assertEqual(task.stage_detail["countdown_seconds"], 12)
        self.service.store.save_progress.assert_called_once()
        self.service.store.save_with_event.assert_not_called()
        self.service.events.emit.assert_called()

    def test_illegal_transition_logged_then_forced(self):
        task = make_task(state="completed", stage="completed")
        self.service._transition_stage(task, "downloading", source="test")
        self.assertEqual(task.stage, "downloading")
        entry = task.stage_history[-1]
        self.assertTrue(entry["forced"])
        # The disqualifying condition must be logged, never silently applied.
        messages = [c[0][3] for c in self.service._log_task.call_args_list]
        self.assertTrue(any("Illegal stage transition" in m for m in messages))
        self.assertTrue(any(c[0][2] == "lifecycle" for c in self.service._log_task.call_args_list))

    def test_history_is_bounded(self):
        task = make_task(state="downloading", stage="downloading")
        for i in range(lifecycle.MAX_STAGE_HISTORY + 10):
            task.stage_history = (task.stage_history or []) + [
                {"from": "downloading", "to": "downloading", "entered_at": float(i)}]
        self.service._transition_stage(task, "verifying_integrity", source="test")
        self.assertLessEqual(len(task.stage_history), lifecycle.MAX_STAGE_HISTORY)

    def test_sensitive_detail_keys_redacted(self):
        task = make_task(state="resolving", stage="captcha_solving")
        self.service._transition_stage(
            task, "captcha_verifying",
            {"token": "secret-value", "direct_url": "https://cdn/file", "solver": "clearcote"},
            source="test")
        self.assertIsNone(task.stage_detail["token"])
        self.assertIsNone(task.stage_detail["direct_url"])
        self.assertEqual(task.stage_detail["solver"], "clearcote")


class TestSolverStageMapping(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def _task(self):
        task = make_task(state="resolving")
        self.service.store.get = MagicMock(return_value=task)
        return task

    def test_countdown_maps_to_hoster_wait_timer(self):
        task = self._task()
        self.service._on_solver_stage(task.id, "countdown", {"countdown_seconds": 28})
        self.assertEqual(task.stage, "hoster_wait_timer")
        self.assertEqual(task.stage_detail["countdown_seconds"], 28)
        self.assertEqual(task.user_challenge["solver_stage"], "countdown")

    def test_countdown_tick_refreshes_without_new_event(self):
        task = self._task()
        task.stage = "hoster_wait_timer"
        task.stage_entered_at = lifecycle.now_us()
        self.service.store.save_with_event.reset_mock()
        self.service._on_solver_stage(task.id, "countdown", {"countdown_seconds": 27})
        self.assertEqual(task.stage_detail["countdown_seconds"], 27)
        self.service.store.save_with_event.assert_not_called()
        self.service.store.save_progress.assert_called_once()

    def test_solver_micro_steps_refresh_detail_without_moving_the_stage(self):
        """Progress inside a solve is detail, not a task-level stage change.

        Mapping each browser step onto the task's stage made one CAPTCHA read as
        solving -> getting metadata -> solving -> waiting timer -> getting
        metadata, and produced 10-21 lifecycle transitions per part where four
        suffice.
        """
        for step in ("navigating", "step_advance", "turnstile_detected", "turnstile_solved"):
            task = self._task()
            task.stage = "captcha_solving"
            self.service._on_solver_stage(task.id, step, {})
            self.assertEqual(task.stage, "captcha_solving",
                             f"solver step '{step}' moved the task's lifecycle stage")
            self.assertEqual(task.stage_detail.get("solver_step"), step,
                             f"solver step '{step}' was not recorded in stage detail")

    def test_countdown_still_earns_a_real_stage(self):
        """A genuine wait remains user-visible; only micro-steps were demoted."""
        task = self._task()
        task.stage = "captcha_solving"
        self.service._on_solver_stage(task.id, "countdown", {"countdown_seconds": 9})
        self.assertEqual(task.stage, "hoster_wait_timer")
        self.assertEqual(task.stage_detail["countdown_seconds"], 9)

    def test_primed_maps_to_direct_link_acquired(self):
        task = self._task()
        task.stage = "captcha_verifying"
        self.service._on_solver_stage(task.id, "primed", {"direct_url": "https://cdn/file"})
        self.assertEqual(task.stage, "direct_link_acquired")
        self.assertTrue(task.stage_detail["primed"])
        self.assertIsNone(task.stage_detail.get("direct_url"))

    def test_unmapped_solver_stage_is_logged_not_silent(self):
        task = self._task()
        self.service._on_solver_stage(task.id, "totally_new_stage", {})
        self.service._log_task.assert_called_once()
        self.assertIn("Unmapped solver stage", self.service._log_task.call_args[0][3])


class TestCoordinatorMapping(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_clearcote_active_maps_to_captcha_solving(self):
        task = make_task(state="needs_user", stage="captcha_challenge_detected")
        self.service.store.get = MagicMock(return_value=task)
        self.service._on_captcha_coordinator(task.id, "clearcote_active",
                                             {"challenge_id": "ch-1", "solver": "automated_browser"})
        self.assertEqual(task.stage, "captcha_solving")
        self.assertEqual(task.stage_detail["coordinator_state"], "clearcote_active")
        self.assertEqual(task.stage_detail["challenge_id"], "ch-1")

    def test_manual_required_returns_to_challenge_detected(self):
        task = make_task(state="needs_user", stage="captcha_solving")
        self.service.store.get = MagicMock(return_value=task)
        self.service._on_captcha_coordinator(task.id, "manual_required", {"challenge_id": "ch-1"})
        self.assertEqual(task.stage, "captcha_challenge_detected")
        self.assertTrue(task.stage_detail["manual_required"])


class TestArchiveStageMapping(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.task = make_task(state="completed", stage="completed")
        self.service.store.get = MagicMock(return_value=self.task)
        self.service.store.list = MagicMock(return_value=[self.task])
        self.job = ArchiveJob(
            id="job-1", task_id=self.task.id, input_path="N:\\dl\\Game.part1.rar",
            output_directory="N:\\dl\\Game", state="queued", package_key="pkg-1",
            operation="extract", part_manifest=[{"part_number": i} for i in range(4)],
            expected_size=100)

    def test_archive_queued_maps_to_unraring_pending(self):
        self.service._apply_archive_stage("ArchiveQueued", self.job)
        self.assertEqual(self.task.stage, "unraring_pending")
        self.assertEqual(self.task.stage_detail["parts"], 4)

    def test_archive_completed_maps_to_cleanup_then_completed(self):
        self.task.stage = "unraring_extracting"
        self.service._apply_archive_stage("ArchiveCompleted", self.job)
        self.assertEqual(self.task.stage, "archive_cleanup")
        self.service._apply_archive_cleanup_result(self.job, 4)
        self.assertEqual(self.task.stage, "completed")
        self.assertEqual(self.task.stage_detail["cleanup_deleted_files"], 4)

    def test_archive_failed_refreshes_detail_not_silent(self):
        self.task.stage = "unraring_extracting"
        self.service.store.save_with_event.reset_mock()
        self.job.state = "failed"
        self.service._apply_archive_stage("ArchiveFailed", self.job)
        self.assertEqual(self.task.stage, "unraring_extracting")
        self.assertEqual(self.task.stage_detail["state"], "failed")
        self.service.store.save_progress.assert_called_once()
        self.service.store.save_with_event.assert_not_called()

    def test_orphan_archive_event_logs_structured(self):
        self.service.store.get = MagicMock(return_value=None)
        self.service.store.list = MagicMock(return_value=[])
        from unittest.mock import patch
        with patch("engine.service.telemetry_bus") as bus:
            self.service._apply_archive_stage("ArchiveQueued", self.job)
            self.service.store.save_with_event.assert_not_called()
            self.assertTrue(bus.record.called)
            message = bus.record.call_args.kwargs.get("message", "")
            self.assertIn("ARCHIVE_STAGE_ORPHAN", message)


class TestSaveProgressPreservesState(unittest.TestCase):
    def test_progress_tick_does_not_clobber_pause(self):
        """A stale download-thread task must not revert a concurrent pause."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            store = TaskStore(path)
            try:
                task = DownloadTask(source_url="https://x/f", destination=temp, id="p1", state="downloading")
                store.save(task)
                paused = store.get("p1")
                paused.state = "paused"
                paused.paused_reason = "user"
                store.save(paused)

                # The download thread still holds an object that says "downloading".
                stale = store.get("p1")
                stale.state = "downloading"
                stale.completed_bytes = 1234
                stale.speed_bytes_per_second = 99.0
                store.save_progress(stale)

                reloaded = store.get("p1")
                self.assertEqual(reloaded.state, "paused")
                self.assertEqual(reloaded.completed_bytes, 1234)
                self.assertEqual(reloaded.speed_bytes_per_second, 99.0)
            finally:
                store.close()


class TestStagePersistence(unittest.TestCase):
    def test_stage_fields_roundtrip_through_sqlite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            store = TaskStore(path)
            reopened = None
            try:
                task = DownloadTask(
                    source_url="https://datanodes.to/abc/Game.part1.rar",
                    destination=temp, display_name="Game.part1.rar", state="downloading",
                    stage="hoster_wait_timer",
                    stage_detail={"countdown_seconds": 28, "source": "solver"},
                    stage_entered_at=1757836800.123456,
                    stage_history=[{"from": "resolving_metadata", "to": "hoster_wait_timer",
                                    "entered_at": 1757836800.123456, "forced": False, "source": "solver:countdown"}])
                store.save(task)
                task_id = task.id
            finally:
                store.close()
            reopened = TaskStore(path)
            try:
                loaded = reopened.get(task_id)
                self.assertEqual(loaded.stage, "hoster_wait_timer")
                self.assertEqual(loaded.stage_detail["countdown_seconds"], 28)
                self.assertEqual(loaded.stage_entered_at, 1757836800.123456)
                self.assertEqual(len(loaded.stage_history), 1)
                self.assertEqual(loaded.stage_history[0]["to"], "hoster_wait_timer")
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
