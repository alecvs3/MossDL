"""Focused tests for the multipart CAPTCHA UX (W1).

Verifies:
1. Exactly one durable challenge per (package_key, host): siblings reuse it.
2. Sibling resume keyed by canonical package key, never folder_path.
3. Repeated solve clicks cannot spawn parallel Clearcote runs.
4. groupId acceptance for canonical keys, legacy folder paths, and bare names.
5. Probe lane release waits for Part 1 to leave resolution.
6. Out-of-order intake holds non-leaders in pending_probe.
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from engine.captcha import CaptchaType
from engine.models import DownloadTask
from engine.models import ResolvedItem
from engine.service import EngineService
from engine import critical_trace

PACKAGE_KEY = "datanodes.to:game"
PAGE_URL = "https://datanodes.to/code1/Game.part1.rar"


def _mp_task(part, state="pending_probe", *, folder_path=None, package_key=PACKAGE_KEY,
             user_challenge=None, package_leader_id=None):
    return DownloadTask(
        id=f"task-{part}",
        source_url=f"https://datanodes.to/code{part}/Game.part{part}.rar",
        destination="/tmp/downloads",
        display_name=f"Game.part{part}.rar",
        folder_path=folder_path,
        package_key=package_key,
        package_part_number=part,
        package_leader_id=package_leader_id,
        state=state,
        user_challenge=user_challenge or {},
    )


class PackageChallengeUnitTests(unittest.TestCase):
    """EngineService.__new__ harness mirroring tests/unit/test_multipart_pipeline.py."""

    def setUp(self):
        self.service = EngineService.__new__(EngineService)
        self.service._last_multipart_probe = {}
        self.service._multipart_probe_delays = {}
        self.service._package_solve_inflight = set()
        self.service._package_solve_lock = threading.Lock()
        self.service.store = MagicMock()
        self.service.store.list.return_value = []
        # The engine re-reads a task before resuming it: look up the fixture's tasks.
        self.service.store.get.side_effect = lambda task_id: next(
            (t for t in self.service.store.list.return_value if t.id == task_id), None)
        self.service.events = MagicMock()
        self.service.captcha = MagicMock()
        self.service.captcha.list_pending_challenges.return_value = []
        self.service.captcha.solve_challenge.return_value = True
        self.service.dispatch = MagicMock()
        self.service.set_host_session = MagicMock()
        self.service._host_sessions = {}
        self.service._futures = {}
        self.service._engine_paused = False

        def mock_transition(task, state, event_type, reason=None):
            task.state = state

        self.service._transition = mock_transition

    def test_single_durable_challenge_per_package(self):
        """The second part reuses the package challenge instead of creating another prompt."""
        created = []
        manager = MagicMock()
        manager.register_challenge.side_effect = lambda c: created.append(c)
        manager.list_pending_challenges.side_effect = lambda: [c.to_dict() for c in created]
        manager.get_challenge.side_effect = lambda cid: next((c for c in created if c.id == cid), None)
        self.service.captcha = manager

        first, created_first = self.service._ensure_package_challenge(
            _mp_task(1, state="resolving"), PACKAGE_KEY, "datanodes.to",
            {"page_url": PAGE_URL}, CaptchaType.TURNSTILE, 90.0, "ch-1")
        second, created_second = self.service._ensure_package_challenge(
            _mp_task(2, state="resolving"), PACKAGE_KEY, "datanodes.to",
            {"page_url": "https://datanodes.to/code2/Game.part2.rar"}, CaptchaType.TURNSTILE, 90.0, "ch-2")

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertIs(first, second)
        self.assertEqual("ch-1", second.id)
        self.assertEqual(1, len({c.id for c in created}), "exactly one challenge may exist per package")
        self.assertEqual(PACKAGE_KEY, first.params["group_id"])
        self.assertEqual("datanodes.to", first.params["origin_host"])
        self.assertEqual("task-1", first.params["leader_task_id"])

    def test_solve_resumes_siblings_by_package_key_without_folder_path(self):
        """folder_path is only set post-resolve; resume must key off the package key."""
        t1 = _mp_task(1, state="needs_user", user_challenge={"challenge_id": "ch-1", "page_url": PAGE_URL})
        t2 = _mp_task(2, state="needs_user", user_challenge={"challenge_id": "ch-1"})
        t3 = _mp_task(3, state="pending_probe", user_challenge={"challenge_id": "ch-1"})
        self.service.store.list.return_value = [t1, t2, t3]

        result = self.service._solve_package_captcha(
            PACKAGE_KEY, "datanodes.to", PAGE_URL, solution={"token": "turnstile-tok"})

        self.assertTrue(result["success"])
        self.assertTrue(result["manual"])
        resumed = [call.args[1]["id"] for call in self.service.dispatch.call_args_list
                   if call.args[0] == "resume_task"]
        self.assertEqual({"task-1", "task-2"}, set(resumed), "only needs_user members resume")
        # Resumed members carry the answer to the site; it is verified, not assumed.
        self.assertTrue(t1.user_challenge.get("verifying"))
        self.assertTrue(t2.user_challenge.get("verifying"))
        self.assertEqual({}, t3.user_challenge)
        # Clearance is shared only once the site accepts the answer (MULTI-02).
        self.service.set_host_session.assert_not_called()
        resolved_calls = [c for c in self.service.events.emit.call_args_list
                          if c.args[0] == "CaptchaChallengeResolved"]
        self.assertEqual(1, len(resolved_calls))
        self.assertEqual(f"package_captcha_resolved:{PACKAGE_KEY}", resolved_calls[0].kwargs["dedupe_key"])

    def test_group_id_accepts_canonical_folder_and_bare_name(self):
        """Older UI builds send folder_path||packageName; the engine must accept all forms."""
        t1 = _mp_task(1, state="needs_user")
        t2 = _mp_task(2, state="pending_probe", folder_path="/downloads/Game", package_key="/downloads/Game")
        self.service.store.list.return_value = [t1, t2]

        for ref in (PACKAGE_KEY, "/downloads/Game", "Game"):
            canonical, members = self.service._resolve_package_reference(ref, "datanodes.to")
            self.assertEqual(PACKAGE_KEY, canonical, f"ref={ref!r}")
            self.assertEqual(2, len(members), f"ref={ref!r}")

    def test_provider_id_host_hint_is_corrected_from_package(self):
        """The UI sends provider ids (datanodes); origin host and package key must still resolve."""
        t1 = _mp_task(1, state="needs_user", user_challenge={"challenge_id": "ch-1"})
        t2 = _mp_task(2, state="pending_probe")
        self.service.store.list.return_value = [t1, t2]

        result = self.service._solve_package_captcha("Game", "datanodes", PAGE_URL,
                                                     solution={"token": "tok"})

        self.assertTrue(result["success"])
        self.assertEqual(PACKAGE_KEY, result["group_id"])
        resumed = [call.args[1]["id"] for call in self.service.dispatch.call_args_list
                   if call.args[0] == "resume_task"]
        self.assertEqual({"task-1"}, set(resumed))
        self.service.set_host_session.assert_not_called()

    def test_repeated_solve_clicks_do_not_double_dispatch(self):
        """A second click while Clearcote is running is deduped, not a second browser run."""
        t1 = _mp_task(1, state="needs_user", user_challenge={"challenge_id": "ch-1"})
        self.service.store.list.return_value = [t1]
        started = threading.Event()
        release = threading.Event()
        calls = []

        def fake_solve(page_url, timeout, cookies, task_id):
            calls.append(task_id)
            started.set()
            release.wait(5.0)
            return {"success": True, "cookies": {"cf_clearance": "cf-1"},
                    "user_agent": "UA", "turnstile_token": "tok-1"}

        with patch("engine.browser_solver.solver_daemon.solve_challenge_sync", side_effect=fake_solve):
            first = self.service._solve_package_captcha("Game", "datanodes.to", PAGE_URL)
            self.assertTrue(first["started"])
            self.assertTrue(started.wait(2.0), "solver worker never started")
            second = self.service._solve_package_captcha("Game", "datanodes.to", PAGE_URL)
            self.assertFalse(second["started"])
            self.assertTrue(second.get("deduped"))
            release.set()
            deadline = time.time() + 5.0
            while self.service._package_solve_inflight and time.time() < deadline:
                time.sleep(0.01)

        self.assertEqual(1, len(calls), "repeated clicks must not start parallel Clearcote runs")
        self.assertEqual(set(), self.service._package_solve_inflight)

    def test_probe_release_waits_for_part1_to_leave_resolution(self):
        """Siblings must never probe while Part 1 is resolving, preflight, or challenged."""
        t1 = _mp_task(1, state="resolving")
        t2 = _mp_task(2, state="pending_probe")
        self.service.store.list.return_value = [t1, t2]

        self.service._release_multipart_probe_lanes(self.service._multipart_package_map([t1, t2]), 1000.0)
        self.assertEqual("pending_probe", t2.state, "resolving Part 1 must hold siblings")

        t1.state = "preflight"
        self.service._release_multipart_probe_lanes(self.service._multipart_package_map([t1, t2]), 1001.0)
        self.assertEqual("pending_probe", t2.state, "preflight Part 1 must hold siblings")

        t1.state = "needs_user"
        self.service._release_multipart_probe_lanes(self.service._multipart_package_map([t1, t2]), 1002.0)
        self.assertEqual("pending_probe", t2.state, "challenged Part 1 must hold siblings")

        t1.state = "downloading"
        self.service._last_multipart_probe.clear()
        self.service._multipart_probe_delays.clear()
        self.service._release_multipart_probe_lanes(self.service._multipart_package_map([t1, t2]), 1003.0)
        self.assertEqual("queued", t2.state, "Part 1 downloading releases the sibling lane")

    def test_prune_probe_state_when_package_terminal(self):
        t1 = _mp_task(1, state="completed")
        t2 = _mp_task(2, state="completed")
        self.service.store.list.return_value = [t1, t2]
        self.service._last_multipart_probe[PACKAGE_KEY] = 1.0
        self.service._multipart_probe_delays[PACKAGE_KEY] = 0.5

        self.service._release_multipart_probe_lanes(self.service._multipart_package_map([t1, t2]), 1000.0)

        self.assertNotIn(PACKAGE_KEY, self.service._last_multipart_probe)
        self.assertNotIn(PACKAGE_KEY, self.service._multipart_probe_delays)


class TestOutOfOrderIntake(unittest.TestCase):
    def test_part_added_before_part1_waits_in_pending_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            service = EngineService(Path(temp))
            try:
                service._engine_paused = True
                t2 = service.dispatch("add_task", {
                    "url": "https://datanodes.to/def/Archive.part2.rar",
                    "destination": temp,
                    "display_name": "Archive.part2.rar",
                })
                self.assertEqual("pending_probe", t2["state"])
                self.assertIn("Waiting for Part 1", t2.get("error", ""))

                t1 = service.dispatch("add_task", {
                    "url": "https://datanodes.to/abc/Archive.part1.rar",
                    "destination": temp,
                    "display_name": "Archive.part1.rar",
                })
                self.assertEqual("queued", t1["state"])

                refreshed = service.store.get(t2["id"])
                self.assertEqual("pending_probe", refreshed.state)
                self.assertEqual(t1["id"], refreshed.package_leader_id)
            finally:
                service.close()

    def test_download_admission_reuses_same_proven_predicate_as_probe_release(self):
        with tempfile.TemporaryDirectory() as temp:
            service = EngineService(Path(temp) / "engine")
            trace_started = False
            try:
                service.dispatch("pause_engine")
                critical_trace.start(Path(temp) / "traces", run_id="admission")
                trace_started = True
                t1 = _mp_task(1, state="preflight")
                t1.browser_context = {"diagnostic_resolution_only": True}
                t1.resolved = [ResolvedItem(
                    provider="datanodes", source_url=t1.source_url,
                    display_name=t1.display_name, direct_url="https://node.datanodes.to/file",
                )]
                t2 = _mp_task(2, state="pending_probe", package_leader_id=t1.id)
                service.store.save(t1)
                service.store.save(t2)

                result = service.dispatch("download_task", {"id": t2.id})

                self.assertEqual("queued", result["state"])
                self.assertNotEqual("pending_probe", service.store.get(t2.id).state)
            finally:
                if trace_started and critical_trace.active():
                    critical_trace.stop()
                service.close()


if __name__ == "__main__":
    unittest.main()
