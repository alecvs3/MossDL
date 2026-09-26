"""When the sentinel part releases its siblings to start pre-work.

Measured (FFT_The_Ivalice_Chronicles, 3 parts, 2026-09-17):

    part1  CAPTCHA 41s + resolve 12s = 53s pre-work, then 51s downloading
    part2  released only once part1 was DOWNLOADING, so its own 53s of pre-work
           finished at almost exactly the moment part1's download ended

The host ceiling was 4 and only ever one stream was open. Nothing was capped:
the two pipeline stages are simply the same length, so each part became ready
exactly as the previous finished. Releasing siblings when the challenge is
CLEARED instead overlaps their pre-work with part 1's transfer.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock

from engine.http_client import clearance_cache
from engine.models import DownloadTask
from engine.service import EngineService

HOST_URL = "https://datanodes.to/abc/Game.part1.rar"
COOKIES = {"cf_clearance": "tok"}


def _part1(state: str, challenge: dict | None = None) -> DownloadTask:
    return DownloadTask(
        id="task-1",
        source_url=HOST_URL,
        destination="/tmp",
        display_name="Game.part1.rar",
        state=state,
        user_challenge=challenge or {},
    )


class SentinelReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = EngineService.__new__(EngineService)
        self.service.store = MagicMock()
        self.service._package_solve_lock = threading.Lock()
        clearance_cache.clear()
        self.addCleanup(clearance_cache.clear)

    def test_downloading_part1_still_releases(self) -> None:
        self.assertTrue(self.service._part1_proven(_part1("downloading")))

    def test_completed_part1_still_releases(self) -> None:
        self.assertTrue(self.service._part1_proven(_part1("completed")))

    def test_part1_awaiting_the_user_does_not_release(self) -> None:
        """Nothing is proven while a human still has to act."""
        self.assertFalse(self.service._part1_proven(_part1("needs_user", {"challenge_id": "c1"})))

    def test_authorized_active_solve_releases_siblings(self) -> None:
        """One user action starts sibling preparation while part 1 is solving."""
        self.service._package_autosolve_armed = {"datanodes.to:game"}
        self.assertTrue(self.service._part1_proven(_part1(
            "needs_user", {"challenge_id": "c1", "solver_active": True},
        )))

    def test_part1_mid_solve_does_not_release(self) -> None:
        self.assertFalse(
            self.service._part1_proven(_part1("resolving", {"challenge_id": "c1", "solver_active": True})),
            "released siblings while part 1 was still solving; a dead package would burn every CAPTCHA")

    def test_cleared_challenge_releases_before_downloading(self) -> None:
        """The point of the change: overlap sibling pre-work with part 1's transfer."""
        clearance_cache.set_clearance(HOST_URL, COOKIES, "UA")
        self.assertTrue(
            self.service._part1_proven(_part1("resolving", {"challenge_id": "c1", "solver_active": False})),
            "sibling pre-work still had to wait for bytes to flow")

    def test_no_clearance_means_not_proven(self) -> None:
        self.assertFalse(self.service._part1_proven(_part1("resolving")))

    def test_unparseable_source_is_not_proven(self) -> None:
        task = _part1("resolving")
        task.source_url = "not-a-url"
        self.assertFalse(self.service._part1_proven(task))


if __name__ == "__main__":
    unittest.main()
