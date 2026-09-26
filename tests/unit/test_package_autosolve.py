"""One click must arm auto-solve for the whole multipart package.

Hosts like DataNodes issue a Turnstile per FILE, so a package raises a fresh
challenge for every sibling. The multipart branch deliberately suppresses the
headless solver cascade and parks each challenge in `needs_user`, which meant
the user had to click Solve once per part. Solving the package once now
authorises the rest.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock

from engine.service import EngineService

PACKAGE_KEY = "datanodes.to:game"
PAGE_URL = "https://datanodes.to/code1/Game.part1.rar"


class ArmedPackageAutosolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = EngineService.__new__(EngineService)
        self.service._package_solve_inflight = set()
        self.service._package_solve_lock = threading.Lock()
        self.service.store = MagicMock()
        self.service.events = MagicMock()

    def test_packages_start_unarmed(self) -> None:
        self.assertEqual(self.service._armed_packages(), set())

    def test_armed_set_is_created_lazily_and_reused(self) -> None:
        first = self.service._armed_packages()
        first.add(PACKAGE_KEY)
        self.assertIs(self.service._armed_packages(), first)
        self.assertIn(PACKAGE_KEY, self.service._armed_packages())

    def test_autosolve_runs_the_package_solver_off_thread(self) -> None:
        done = threading.Event()
        seen: list[tuple] = []

        def _solver(pkey, host, page_url, **kwargs):
            seen.append((pkey, host, page_url))
            done.set()
            return {"success": True}

        self.service._solve_package_captcha = _solver
        self.service._autosolve_armed_package(PACKAGE_KEY, "datanodes.to", PAGE_URL)
        self.assertTrue(done.wait(5), "armed auto-solve never invoked the package solver")
        self.assertEqual(seen, [(PACKAGE_KEY, "datanodes.to", PAGE_URL)])

    def test_autosolve_failure_is_reported_never_silent(self) -> None:
        """A failed auto-solve must be announced: nobody is watching for a click."""
        done = threading.Event()

        def _boom(*_args, **_kwargs):
            done.set()
            raise RuntimeError("clearcote unavailable")

        self.service._solve_package_captcha = _boom
        records: list[str] = []
        from engine import service as service_module

        original = service_module.telemetry_bus.record

        def _capture(**kwargs):
            records.append(str(kwargs.get("message", "")))
            return original(**kwargs)

        service_module.telemetry_bus.record = _capture
        try:
            self.service._autosolve_armed_package(PACKAGE_KEY, "datanodes.to", PAGE_URL)
            self.assertTrue(done.wait(5))
            for _ in range(50):
                if any("PACKAGE_AUTOSOLVE_FAILED" in message for message in records):
                    break
                threading.Event().wait(0.05)
        finally:
            service_module.telemetry_bus.record = original

        self.assertTrue(
            any("PACKAGE_AUTOSOLVE_FAILED" in message for message in records),
            f"auto-solve failure was swallowed; records={records}")
        self.assertTrue(
            any("PACKAGE_AUTOSOLVE_ARMED" in message for message in records),
            "arming itself should be visible in telemetry")


class PackageChallengeForkTests(unittest.TestCase):
    """Siblings must fork off their own challenge once one is being solved.

    A package shares CLEARANCE, not the per-file token. Handing every sibling the
    same challenge object meant all parts deduped onto one id, so exactly one
    solve ran while the other two solver lanes idled.
    """

    def setUp(self) -> None:
        self.service = EngineService.__new__(EngineService)
        self.service._package_solve_inflight = set()
        self.service._package_solve_lock = threading.Lock()

    def _challenge(self, challenge_id: str, task_id: str):
        from engine.captcha import CaptchaChallenge, CaptchaType

        return CaptchaChallenge(
            id=challenge_id, task_id=task_id, provider_id="datanodes",
            captcha_type=CaptchaType.TURNSTILE, params={"group_id": PACKAGE_KEY},
            timeout_seconds=90.0,
        )

    def _task(self, task_id: str):
        from engine.models import DownloadTask

        return DownloadTask(id=task_id, source_url="https://datanodes.to/x/Game.part2.rar",
                            destination="/tmp", display_name="Game.part2.rar")

    def test_owner_always_reuses_its_own_challenge(self) -> None:
        challenge = self._challenge("chal-1", "task-1")
        self.assertTrue(
            self.service._package_challenge_is_reusable(challenge, self._task("task-1"), PACKAGE_KEY))

    def test_sibling_shares_an_idle_challenge(self) -> None:
        """Nothing is solving it yet, so it is still the same pending work."""
        challenge = self._challenge("chal-1", "task-1")
        self.assertTrue(
            self.service._package_challenge_is_reusable(challenge, self._task("task-2"), PACKAGE_KEY))

    def test_sibling_forks_when_a_solve_is_in_flight(self) -> None:
        challenge = self._challenge("chal-1", "task-1")
        self.service._package_solve_inflight.add(f"{PACKAGE_KEY}:chal-1")
        self.assertFalse(
            self.service._package_challenge_is_reusable(challenge, self._task("task-2"), PACKAGE_KEY),
            "sibling adopted a challenge whose token belongs to another file; "
            "its solve would be deduped away and the part would stall")

    def test_sibling_forks_for_a_legacy_package_keyed_solve(self) -> None:
        challenge = self._challenge("chal-1", "task-1")
        self.service._package_solve_inflight.add(PACKAGE_KEY)
        self.assertFalse(
            self.service._package_challenge_is_reusable(challenge, self._task("task-2"), PACKAGE_KEY))

    def test_stale_solver_result_does_not_resume_completed_owner_or_sibling(self) -> None:
        """A late per-file token must not restart preflight or leak to a sibling."""
        from engine.models import DownloadTask

        stale_owner = DownloadTask(
            id="task-1", source_url=PAGE_URL, destination="/tmp",
            display_name="Game.part1.rar", state="needs_user",
        )
        stale_sibling = DownloadTask(
            id="task-2", source_url="https://datanodes.to/code2/Game.part2.rar",
            destination="/tmp", display_name="Game.part2.rar", state="needs_user",
        )
        current_owner = DownloadTask(
            id="task-1", source_url=PAGE_URL, destination="/tmp",
            display_name="Game.part1.rar", state="preflight",
        )
        current_sibling = DownloadTask(
            id="task-2", source_url=stale_sibling.source_url, destination="/tmp",
            display_name="Game.part2.rar", state="needs_user",
        )
        challenge = self._challenge("chal-1", "task-1")
        self.service.store = MagicMock()
        self.service.events = MagicMock()
        self.service.captcha = MagicMock()
        self.service.set_host_session = MagicMock()
        self.service.dispatch = MagicMock()
        self.service.store.get.side_effect = lambda task_id: {
            "task-1": current_owner, "task-2": current_sibling,
        }[task_id]

        self.service._apply_package_captcha_clearance(
            PACKAGE_KEY, "datanodes.to", {"turnstile_token": "single-use"},
            "clearcote", members=[stale_owner, stale_sibling], challenge=challenge,
        )

        self.service.dispatch.assert_not_called()
        self.assertEqual(current_owner.state, "preflight")
        self.assertEqual(current_sibling.state, "needs_user")

    def test_per_file_solution_resumes_only_challenge_owner(self) -> None:
        owner = self._task("task-1")
        owner.state = "needs_user"
        sibling = self._task("task-2")
        sibling.state = "needs_user"
        challenge = self._challenge("chal-1", owner.id)
        self.service.store = MagicMock()
        self.service.events = MagicMock()
        self.service.captcha = MagicMock()
        self.service.set_host_session = MagicMock()
        self.service.dispatch = MagicMock()
        self.service.store.get.side_effect = lambda task_id: {owner.id: owner, sibling.id: sibling}[task_id]

        self.service._apply_package_captcha_clearance(
            PACKAGE_KEY, "datanodes.to", {"turnstile_token": "owner-token"},
            "clearcote", members=[owner, sibling], challenge=challenge,
        )

        resumed_ids = [call.args[1]["id"] for call in self.service.dispatch.call_args_list]
        self.assertEqual(resumed_ids, [owner.id])
        self.assertEqual(sibling.state, "needs_user")


class PerChallengeDedupeTests(unittest.TestCase):
    """Distinct sibling challenges must each be solved, not collapsed.

    Keying the in-flight set on the package alone silently dropped every sibling
    after the first: hosts that issue a Turnstile per file raise one challenge
    per part, so all but one part stayed parked forever.
    """

    def setUp(self) -> None:
        self.service = EngineService.__new__(EngineService)
        self.service._package_solve_inflight = set()
        self.service._package_solve_lock = threading.Lock()
        self.service.store = MagicMock()
        self.service.events = MagicMock()
        self.service.captcha = MagicMock()
        self.service.captcha.get_challenge.return_value = None
        self.service._resolve_package_reference = lambda ref, host: (PACKAGE_KEY, [])
        self.service._package_key_base = lambda ref: "game"
        self.service._package_host_hint = lambda members: "datanodes.to"
        self.service._package_leader_id = lambda *a, **k: "task-1"
        self.service._package_page_url = lambda *a, **k: PAGE_URL
        self.service._find_package_challenge = lambda *a, **k: None
        self.service._fail_package_captcha = MagicMock()
        self.service._apply_package_captcha_clearance = MagicMock(return_value=True)

        self.started = threading.Semaphore(0)
        self.release = threading.Event()
        self.solved: list[str] = []

        def _solve_sync(page_url, timeout, cookies, task_id):
            self.solved.append(str(task_id))
            self.started.release()
            self.release.wait(5)
            return {"success": True, "cookies": {}, "turnstile_token": "tok"}

        from engine import browser_solver

        self._daemon = browser_solver.solver_daemon
        self._original = self._daemon.solve_challenge_sync
        self._daemon.solve_challenge_sync = _solve_sync
        self.addCleanup(setattr, self._daemon, "solve_challenge_sync", self._original)
        self.addCleanup(self.release.set)

    def test_same_challenge_twice_is_deduped(self) -> None:
        first = self.service._solve_package_captcha(PACKAGE_KEY, "datanodes.to", PAGE_URL,
                                                    challenge_id="chal-1")
        self.assertTrue(self.started.acquire(timeout=5), "first solve never started")
        second = self.service._solve_package_captcha(PACKAGE_KEY, "datanodes.to", PAGE_URL,
                                                     challenge_id="chal-1")
        self.assertTrue(first.get("started"))
        self.assertTrue(second.get("deduped"), "a repeated click must not spawn a second Clearcote run")
        self.release.set()

    def test_distinct_sibling_challenges_both_run(self) -> None:
        first = self.service._solve_package_captcha(PACKAGE_KEY, "datanodes.to", PAGE_URL,
                                                    challenge_id="chal-1")
        self.assertTrue(self.started.acquire(timeout=5), "first solve never started")
        second = self.service._solve_package_captcha(PACKAGE_KEY, "datanodes.to", PAGE_URL,
                                                     challenge_id="chal-2")
        self.assertTrue(self.started.acquire(timeout=5),
                        "second sibling challenge was deduped away and would stay parked forever")
        self.assertTrue(first.get("started"))
        self.assertTrue(second.get("started"))
        self.assertFalse(second.get("deduped"))
        self.release.set()


if __name__ == "__main__":
    unittest.main()
