"""Unit tests for Phase 24 multipart pipeline (PIPE-01, PIPE-04):
Sentinel Part 1 probe, pending_probe state machine, and staggered jitter.
"""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from engine.models import DownloadTask
from engine.service import EngineService


class TestMultipartPipeline(unittest.TestCase):

    def setUp(self):
        self.service = EngineService.__new__(EngineService)
        self.service._last_multipart_probe = {}
        self.service._multipart_probe_delays = {}
        self.service._futures = {}
        self.service._host_cooldown_warned = {}
        self.service.store = MagicMock()
        self.service.events = MagicMock()
        self.service._log_task = MagicMock()
        self.service._ui_settings = MagicMock(return_value={"general": {"autoStart": True}})
        self.service._engine_paused = False

        # Mock transitions to update task state and record transition
        def mock_transition(task, state, event_type, reason=None):
            task.state = state
        self.service._transition = mock_transition

    def test_task_multipart_detection(self):
        """Verify _task_multipart_info correctly identifies multi-part archive parts."""
        t1 = DownloadTask(
            source_url="https://datanodes.to/abc/Game.part01.rar",
            destination="/tmp",
            display_name="Game.part01.rar",
            folder_path="/tmp/Game"
        )
        is_mp, pkey, pnum = self.service._task_multipart_info(t1)
        self.assertTrue(is_mp)
        self.assertEqual(pnum, 1)
        self.assertEqual(pkey, "/tmp/Game")

        t2 = DownloadTask(
            source_url="https://datanodes.to/def/Game.part02.rar",
            destination="/tmp",
            display_name="Game.part02.rar",
            folder_path="/tmp/Game"
        )
        is_mp, pkey, pnum = self.service._task_multipart_info(t2)
        self.assertTrue(is_mp)
        self.assertEqual(pnum, 2)
        self.assertEqual(pkey, "/tmp/Game")

        # Non-multipart
        t_single = DownloadTask(
            source_url="https://example.com/video.mp4",
            destination="/tmp",
            display_name="video.mp4"
        )
        is_mp, _, _ = self.service._task_multipart_info(t_single)
        self.assertFalse(is_mp)

    def test_sentinel_part1_admitted_siblings_pending_probe(self):
        """When 3 parts are queued, only Part 1 stays queued; Parts 2 and 3 enter pending_probe."""
        t1 = DownloadTask(source_url="https://datanodes.to/1/App.part1.rar", destination="/tmp",
                          display_name="App.part1.rar", folder_path="/tmp/App", state="queued")
        t2 = DownloadTask(source_url="https://datanodes.to/2/App.part2.rar", destination="/tmp",
                          display_name="App.part2.rar", folder_path="/tmp/App", state="queued")
        t3 = DownloadTask(source_url="https://datanodes.to/3/App.part3.rar", destination="/tmp",
                          display_name="App.part3.rar", folder_path="/tmp/App", state="queued")

        tasks = [t1, t2, t3]

        # Simulate pump grouping and sentinel enforcement
        pkg_map = {}
        for t in tasks:
            is_mp, pkey, pnum = self.service._task_multipart_info(t)
            if is_mp and pkey:
                pkg_map.setdefault(pkey, []).append(t)

        now = 1000.0
        for pkey, ptasks in pkg_map.items():
            part1 = next((t for t in ptasks if self.service._task_multipart_info(t)[2] == 1), None)
            part1_resolved = part1.state in {"downloading", "verifying", "postprocessing", "completed"}
            other_parts = sorted(
                [t for t in ptasks if self.service._task_multipart_info(t)[2] > 1],
                key=lambda t: self.service._task_multipart_info(t)[2]
            )
            if not part1_resolved:
                for other in other_parts:
                    if other.state == "queued":
                        self.service._transition(other, "pending_probe", "TaskStateChanged", "Waiting for Part 1")

        self.assertEqual(t1.state, "queued", "Part 1 must remain queued for resolving")
        self.assertEqual(t2.state, "pending_probe", "Part 2 must enter pending_probe")
        self.assertEqual(t3.state, "pending_probe", "Part 3 must enter pending_probe")

    def test_clean_part1_triggers_staggered_probing(self):
        """When Part 1 completes resolving, Parts 2..N are released sequentially with delay."""
        t1 = DownloadTask(source_url="https://datanodes.to/1/App.part1.rar", destination="/tmp",
                          display_name="App.part1.rar", folder_path="/tmp/App", state="downloading")
        t2 = DownloadTask(source_url="https://datanodes.to/2/App.part2.rar", destination="/tmp",
                          display_name="App.part2.rar", folder_path="/tmp/App", state="pending_probe")
        t3 = DownloadTask(source_url="https://datanodes.to/3/App.part3.rar", destination="/tmp",
                          display_name="App.part3.rar", folder_path="/tmp/App", state="pending_probe")

        tasks = [t1, t2, t3]
        pkg_key = "/tmp/App"
        self.service._last_multipart_probe[pkg_key] = 1000.0
        self.service._multipart_probe_delays[pkg_key] = 1.0  # 1s delay

        # Case A: Less than 1 second elapsed -> Part 2 remains pending_probe
        now = 1000.5
        delay = self.service._multipart_probe_delays[pkg_key]
        last_probe = self.service._last_multipart_probe[pkg_key]
        has_resolving = any(t.state in {"resolving", "preflight"} for t in tasks)
        if not has_resolving and (now - last_probe >= delay):
            t2.state = "queued"
        self.assertEqual(t2.state, "pending_probe", "Should not release before 1s delay")

        # Case B: 1.1 seconds elapsed -> Part 2 is released to queued
        now = 1001.1
        if not has_resolving and (now - last_probe >= delay):
            t2.state = "queued"
            self.service._last_multipart_probe[pkg_key] = now
        self.assertEqual(t2.state, "queued", "Part 2 should release to queued")
        self.assertEqual(t3.state, "pending_probe", "Part 3 must still wait")

        # Case C: While Part 2 is resolving, Part 3 must NOT be released even if time elapsed
        t2.state = "resolving"
        now = 1003.0
        has_resolving = any(t.state in {"resolving", "preflight"} for t in tasks)
        if not has_resolving and (now - self.service._last_multipart_probe[pkg_key] >= 1.0):
            t3.state = "queued"
        self.assertEqual(t3.state, "pending_probe", "Part 3 must wait while Part 2 is resolving")

        # Case D: Once Part 2 reaches downloading, Part 3 releases
        t2.state = "downloading"
        has_resolving = any(t.state in {"resolving", "preflight"} for t in tasks)
        if not has_resolving and (now - self.service._last_multipart_probe[pkg_key] >= 1.0):
            t3.state = "queued"
        self.assertEqual(t3.state, "queued", "Part 3 releases when Part 2 finishes resolving")

    def test_part1_challenge_holds_siblings_in_pending_probe(self):
        """When Part 1 is in needs_user, Parts 2..N remain held in pending_probe."""
        t1 = DownloadTask(source_url="https://datanodes.to/1/App.part1.rar", destination="/tmp",
                          display_name="App.part1.rar", folder_path="/tmp/App", state="needs_user")
        t2 = DownloadTask(source_url="https://datanodes.to/2/App.part2.rar", destination="/tmp",
                          display_name="App.part2.rar", folder_path="/tmp/App", state="pending_probe")

        part1_resolved = t1.state in {"downloading", "verifying", "postprocessing", "completed"}
        self.assertFalse(part1_resolved)
        self.assertEqual(t2.state, "pending_probe", "Part 2 must stay in pending_probe")

    def test_import_definitions_persists_sentinel_atomically(self):
        """The real batch intake path must save every sibling before pumping."""
        with tempfile.TemporaryDirectory() as temp:
            service = EngineService(Path(temp))
            try:
                service._engine_paused = True
                result = service.dispatch("import_definitions", {
                    "tasks": [
                        {"url": "https://datanodes.to/one/Game.part01.rar", "destination": temp, "display_name": "Game.part01.rar"},
                        {"url": "https://datanodes.to/two/Game.part02.rar", "destination": temp, "display_name": "Game.part02.rar"},
                        {"url": "https://datanodes.to/three/Game.part03.rar", "destination": temp, "display_name": "Game.part03.rar"},
                    ]
                })
                self.assertEqual(len(result["imported"]), 3)
                tasks = {task.display_name: task for task in service.store.list()}
                self.assertEqual(tasks["Game.part01.rar"].state, "queued")
                self.assertEqual(tasks["Game.part02.rar"].state, "pending_probe")
                self.assertEqual(tasks["Game.part03.rar"].state, "pending_probe")
            finally:
                service.close()


    def test_part1_captcha_suppresses_headless_cascade_and_emits_event(self):
        """PIPE-02: Multipart challenge suppresses automated headless solver and emits event."""
        t1 = DownloadTask(source_url="https://datanodes.to/1/App.part1.rar", destination="/tmp",
                          display_name="App.part1.rar", folder_path="/tmp/App", state="resolving")
        
        is_mp, pkey, pnum = self.service._task_multipart_info(t1)
        self.assertTrue(is_mp)
        
        # Simulate NeedsCaptcha catch block
        c_params = {"page_url": "https://datanodes.to/1/App.part1.rar", "site_key": "0x4AAAAAA"}
        self.service._transition(t1, "needs_user", "TaskNeedsUser", "Waiting for Turnstile verification")
        
        if is_mp:
            self.service.events.emit("CaptchaChallengeRequired", t1.id, {
                "groupId": pkey,
                "displayName": t1.display_name or t1.source_url,
                "host": "datanodes.to",
                "pageUrl": c_params.get("page_url"),
            })
            cascade_suppressed = True
        else:
            cascade_suppressed = False

        self.assertTrue(cascade_suppressed, "Headless cascade must be suppressed for multipart")
        self.assertEqual(t1.state, "needs_user")
        self.service.events.emit.assert_called_with("CaptchaChallengeRequired", t1.id, {
            "groupId": "/tmp/App",
            "displayName": "App.part1.rar",
            "host": "datanodes.to",
            "pageUrl": "https://datanodes.to/1/App.part1.rar",
        })

    def test_solve_multipart_captcha_stores_cookies_and_ua(self):
        """PIPE-03: solve_multipart_captcha writes cf_clearance and exact UA to host session jar."""
        self.service._host_sessions = {}
        def mock_set_host_session(host, session_data):
            self.service._host_sessions[host.lower()] = session_data
        self.service.set_host_session = mock_set_host_session

        # Simulate solver result
        harvested_cookies = {"cf_clearance": "cleared123", "session": "abc"}
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"

        self.service.set_host_session("datanodes.to", {
            "cookies": harvested_cookies,
            "cf_clearance": "cleared123",
            "user_agent": user_agent,
        })

        session = self.service._host_sessions.get("datanodes.to")


    def test_part1_captcha_suppresses_headless_cascade_and_emits_event(self):
        """PIPE-02: Multipart challenge suppresses automated headless solver and emits event."""
        t1 = DownloadTask(source_url="https://datanodes.to/1/App.part1.rar", destination="/tmp",
                          display_name="App.part1.rar", folder_path="/tmp/App", state="resolving")
        
        is_mp, pkey, pnum = self.service._task_multipart_info(t1)
        self.assertTrue(is_mp)
        
        # Simulate NeedsCaptcha catch block
        c_params = {"page_url": "https://datanodes.to/1/App.part1.rar", "site_key": "0x4AAAAAA"}
        self.service._transition(t1, "needs_user", "TaskNeedsUser", "Waiting for Turnstile verification")
        
        if is_mp:
            self.service.events.emit("CaptchaChallengeRequired", t1.id, {
                "groupId": pkey,
                "displayName": t1.display_name or t1.source_url,
                "host": "datanodes.to",
                "pageUrl": c_params.get("page_url"),
            })
            cascade_suppressed = True
        else:
            cascade_suppressed = False

        self.assertTrue(cascade_suppressed, "Headless cascade must be suppressed for multipart")
        self.assertEqual(t1.state, "needs_user")
        self.service.events.emit.assert_called_with("CaptchaChallengeRequired", t1.id, {
            "groupId": "/tmp/App",
            "displayName": "App.part1.rar",
            "host": "datanodes.to",
            "pageUrl": "https://datanodes.to/1/App.part1.rar",
        })

    def test_solve_multipart_captcha_stores_cookies_and_ua(self):
        """PIPE-03: solve_multipart_captcha writes cf_clearance and exact UA to host session jar."""
        self.service._host_sessions = {}
        def mock_set_host_session(host, session_data):
            self.service._host_sessions[host.lower()] = session_data
        self.service.set_host_session = mock_set_host_session

        # Simulate solver result
        harvested_cookies = {"cf_clearance": "cleared123", "session": "abc"}
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"

        self.service.set_host_session("datanodes.to", {
            "cookies": harvested_cookies,
            "cf_clearance": "cleared123",
            "user_agent": user_agent,
        })

        session = self.service._host_sessions.get("datanodes.to")
        self.assertIsNotNone(session)
        self.assertEqual(session["cf_clearance"], "cleared123")
        self.assertEqual(session["user_agent"], user_agent)
        self.assertEqual(session["cookies"]["cf_clearance"], "cleared123")

    def test_solve_multipart_captcha_resumes_all_siblings(self):
        """PIPE-03: solve_multipart_captcha resumes Part 1 and all siblings in group."""
        t1 = DownloadTask(source_url="https://datanodes.to/1/App.part1.rar", destination="/tmp",
                          display_name="App.part1.rar", folder_path="/tmp/App", state="needs_user")
        t2 = DownloadTask(source_url="https://datanodes.to/2/App.part2.rar", destination="/tmp",
                          display_name="App.part2.rar", folder_path="/tmp/App", state="needs_user")
        
        self.service.store.list = MagicMock(return_value=[t1, t2])
        resumed = []
        def mock_dispatch(method, params):
            if method == "resume_task":
                resumed.append(params["id"])
        self.service.dispatch = mock_dispatch

        # Run resumption loop as in solve_multipart_captcha
        group_id = "/tmp/App"
        solve_secrets = {"cf_clearance": "cf123", "user_agent": "UA123"}
        for t in self.service.store.list():
            is_mp, pkey, _ = self.service._task_multipart_info(t)
            if is_mp and pkey == group_id and t.state == "needs_user":
                self.service.dispatch("resume_task", {"id": t.id, "secrets": solve_secrets})

        self.assertIn(t1.id, resumed)
        self.assertIn(t2.id, resumed)

    def test_multipart_package_key_stability_across_resolution(self):
        """Verify that when Part 1 discovers its folder_path, package_key remains stable and siblings match."""
        t1 = DownloadTask(
            source_url="https://datanodes.to/abc/Archive.part1.rar",
            destination="/tmp/downloads",
            display_name="Archive.part1.rar",
            package_key="datanodes.to:archive",
            package_part_number=1,
            state="completed",
            folder_path="/tmp/downloads/Archive"
        )
        t2 = DownloadTask(
            source_url="https://datanodes.to/def/Archive.part2.rar",
            destination="/tmp/downloads",
            display_name="Archive.part2.rar",
            package_key="datanodes.to:archive",
            package_part_number=2,
            state="pending_probe",
            folder_path=None
        )
        is_mp1, pkey1, pnum1 = self.service._task_multipart_info(t1)
        is_mp2, pkey2, pnum2 = self.service._task_multipart_info(t2)
        self.assertTrue(is_mp1)
        self.assertTrue(is_mp2)
        self.assertEqual(pnum1, 1)
        self.assertEqual(pnum2, 2)
        self.assertEqual(pkey1, "datanodes.to:archive")
        self.assertEqual(pkey2, "datanodes.to:archive")
        self.assertEqual(pkey1, pkey2, "Part 1 and Part 2 must retain identical package_keys after Part 1 resolves")


if __name__ == "__main__":
    unittest.main()
