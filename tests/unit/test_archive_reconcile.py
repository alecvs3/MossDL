"""A finished multipart package must never sit with no archive job.

Observed live (FFT_The_Ivalice_Chronicles, 3 parts, 2026-09-17): all three parts
completed and verified, all three .rar files on disk, `is_package_complete`
returning True -- and zero archive jobs, zero archive events, and no log line
explaining why. Archiving is triggered by the completion of the LAST part, so a
single missed event strands the package silently and nothing retries.
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import MagicMock

from engine.models import DownloadTask
from engine.service import EngineService

PKEY = "datanodes.to:game_--_example-repacks.test_--_"


def _part(number: int, state: str = "completed", integrity: str = "size_verified") -> DownloadTask:
    return DownloadTask(
        id=f"task-{number}",
        source_url=f"https://datanodes.to/x{number}/Game.part{number}.rar",
        destination=r"D:\Downloads\Game",
        display_name=f"Game.part{number}.rar",
        state=state,
        integrity_state=integrity,
        package_key=PKEY,
        package_part_number=number,
    )


class ReconcileStrandedArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = EngineService.__new__(EngineService)
        self.service.store = MagicMock()
        self.service.store.list_archive_jobs.return_value = []
        self.service._package_solve_lock = threading.Lock()
        self.queued: list[DownloadTask] = []

        async def _fake_completed(task):
            self.queued.append(task)

        self.service._on_download_completed = _fake_completed

    def _run(self) -> None:
        asyncio.run(self.service._reconcile_stranded_archives())

    def test_finished_package_with_no_job_is_queued(self) -> None:
        """The exact live failure: 3 parts done, no archive job, nothing retries."""
        self.service.store.list.return_value = [_part(1), _part(2), _part(3)]
        self._run()
        self.assertEqual(len(self.queued), 1, "a stranded finished package was not reconciled")
        self.assertEqual(self.queued[0].package_key, PKEY)

    def test_package_with_a_completed_job_is_left_alone(self) -> None:
        self.service.store.list.return_value = [_part(1), _part(2)]
        self.service.store.list_archive_jobs.return_value = [
            {"package_key": PKEY, "state": "completed"}]
        self._run()
        self.assertEqual(self.queued, [], "reconciliation queued a duplicate archive job")

    def test_package_with_an_inflight_job_is_left_alone(self) -> None:
        for state in ("queued", "running", "extracting", "cleanup_pending"):
            with self.subTest(state=state):
                self.queued.clear()
                self.service._archive_retry_attempted = set()
                self.service.store.list.return_value = [_part(1), _part(2)]
                self.service.store.list_archive_jobs.return_value = [
                    {"package_key": PKEY, "state": state}]
                self._run()
                self.assertEqual(self.queued, [], f"interrupted a job in state '{state}'")

    def test_failed_job_does_not_block_the_package_forever(self) -> None:
        """The live failure: an archive-worker crash disabled unrar for that package.

        Every later re-download skipped archiving because a job "already
        existed", so the user saw unrar as simply not working.
        """
        self.service.store.list.return_value = [_part(1), _part(2)]
        self.service.store.list_archive_jobs.return_value = [
            {"package_key": PKEY, "state": "failed"}]
        self._run()
        self.assertEqual(len(self.queued), 1, "a failed archive job blocked the package permanently")

    def test_failed_job_is_retried_only_once_per_run(self) -> None:
        """Retry, but never in a loop: the sweep runs every couple of seconds."""
        self.service.store.list.return_value = [_part(1), _part(2)]
        self.service.store.list_archive_jobs.return_value = [
            {"package_key": PKEY, "state": "failed"}]
        self._run()
        self._run()
        self._run()
        self.assertEqual(len(self.queued), 1, "a failed job was retried on every sweep")

    def test_unfinished_package_is_left_alone(self) -> None:
        self.service.store.list.return_value = [_part(1), _part(2, state="downloading")]
        self._run()
        self.assertEqual(self.queued, [], "reconciled a package that is still downloading")

    def test_unverified_part_blocks_reconciliation(self) -> None:
        self.service.store.list.return_value = [_part(1), _part(2, integrity="corrupt")]
        self._run()
        self.assertEqual(self.queued, [])

    def test_checksum_verified_package_also_reconciles(self) -> None:
        self.service.store.list.return_value = [_part(1, integrity="verified"),
                                                _part(2, integrity="verified")]
        self._run()
        self.assertEqual(len(self.queued), 1)

    def test_single_part_is_not_a_package(self) -> None:
        self.service.store.list.return_value = [_part(1)]
        self._run()
        self.assertEqual(self.queued, [], "a lone part was treated as a multipart package")

    def test_no_packages_is_a_no_op(self) -> None:
        self.service.store.list.return_value = []
        self._run()
        self.assertEqual(self.queued, [])

    def test_job_listing_failure_does_not_raise(self) -> None:
        """The sweep runs on the watchdog tick; it must never take the loop down."""
        self.service.store.list.return_value = [_part(1), _part(2)]
        self.service.store.list_archive_jobs.side_effect = RuntimeError("db locked")
        self._run()
        self.assertEqual(self.queued, [])


if __name__ == "__main__":
    unittest.main()
