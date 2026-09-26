from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import json
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from engine.captcha import (
    CaptchaChallenge,
    CaptchaManager,
    CaptchaStatus,
    CaptchaType,
    LocalOcrSolver,
    ThirdPartyApiSolver,
    BrowserLoopbackSolver,
    InteractiveUiSolver,
)
from engine.db import TaskStore
from engine.events import EventPublisher
from engine.errors import NeedsCaptcha, NeedsUser
from engine.models import DownloadTask
from engine.service import EngineService


class CaptchaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.store = TaskStore(self.data_dir / "test.sqlite3")
        self.manager = CaptchaManager(store=self.store, event_publisher=EventPublisher(self.store))

    def tearDown(self) -> None:
        self.manager.loopback_solver.stop()
        self.store.close()
        self.temp_dir.cleanup()

    def test_challenge_models_and_types(self) -> None:
        challenge = CaptchaChallenge(
            task_id="task-123",
            provider_id="rapidgator",
            captcha_type=CaptchaType.TURNSTILE,
            params={"site_key": "0x4AAAAAA", "page_url": "https://rapidgator.net/file/1"},
            timeout_seconds=60.0,
        )
        self.assertEqual(challenge.captcha_type, CaptchaType.TURNSTILE)
        self.assertFalse(challenge.is_expired())
        self.assertGreater(challenge.time_remaining(), 50.0)

        data = challenge.to_dict()
        self.assertEqual(data["task_id"], "task-123")
        self.assertEqual(data["captcha_type"], "turnstile")
        self.assertEqual(data["status"], "pending")
        self.assertIn("time_remaining", data)

    def test_local_ocr_solver_math_and_heuristics(self) -> None:
        solver = LocalOcrSolver()

        # Math evaluation
        c1 = CaptchaChallenge(
            captcha_type=CaptchaType.IMAGE_TEXT,
            params={"text_prompt": "Solve: 15 + 27 = ?"},
        )
        self.assertTrue(solver.can_solve(c1))
        loop = asyncio.new_event_loop()
        try:
            res1 = loop.run_until_complete(solver.solve(c1))
            self.assertIsNotNone(res1)
            self.assertEqual(res1["text"], "42")
            self.assertEqual(res1["method"], "math_eval")

            # Multiplication
            c2 = CaptchaChallenge(
                captcha_type=CaptchaType.IMAGE_TEXT,
                params={"text_prompt": "What is 6 * 7?"},
            )
            res2 = loop.run_until_complete(solver.solve(c2))
            self.assertEqual(res2["text"], "42")

            # Mock solution payload
            c3 = CaptchaChallenge(
                captcha_type=CaptchaType.IMAGE_TEXT,
                params={"mock_solution": "k9xp7"},
            )
            res3 = loop.run_until_complete(solver.solve(c3))
            self.assertEqual(res3["text"], "k9xp7")

            # Non-image type should not be solvable
            c4 = CaptchaChallenge(captcha_type=CaptchaType.TURNSTILE, params={})
            self.assertFalse(solver.can_solve(c4))
        finally:
            loop.close()

    def test_third_party_api_solver_mock(self) -> None:
        solver = ThirdPartyApiSolver(solver_id="twocaptcha", service_type="twocaptcha", api_key="fake-key-123", enabled=True)

        bad_reports = []

        def mock_client(action: str, params: dict):
            if action == "get_balance":
                return {"balance": 14.50}
            if action == "solve":
                return {"solution": {"token": "token-from-2captcha-xyz"}, "external_id": "ext-999"}
            if action == "report_bad":
                bad_reports.append(params["external_id"])
                return {"status": "ok"}
            return {}

        solver.http_client = mock_client

        loop = asyncio.new_event_loop()
        try:
            # Check balance
            balance = loop.run_until_complete(solver.get_balance())
            self.assertEqual(balance, 14.50)

            # Solve Turnstile
            c = CaptchaChallenge(
                captcha_type=CaptchaType.TURNSTILE,
                params={"site_key": "cf-key", "page_url": "https://example.test"},
            )
            self.assertTrue(solver.can_solve(c))
            sol = loop.run_until_complete(solver.solve(c))
            self.assertEqual(sol["token"], "token-from-2captcha-xyz")

            # Report invalid solution (refund trigger)
            solver.report_result(c, valid=False)
            self.assertEqual(bad_reports, ["ext-999"])
        finally:
            loop.close()

    def test_browser_loopback_solver_and_html(self) -> None:
        loopback = self.manager.loopback_solver
        loopback.start()
        try:
            c = CaptchaChallenge(
                id="test-challenge-loopback",
                provider_id="rapidgator",
                captcha_type=CaptchaType.TURNSTILE,
                params={"site_key": "0x4AAAAAAtest", "action": "download"},
            )
            self.manager._challenges[c.id] = c

            url = loopback.get_url(c.id)
            self.assertTrue(url.startswith("http://127.0.0.1:"))

            # GET HTML challenge page
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                html = resp.read().decode("utf-8")
                self.assertIn("cf-turnstile", html)
                self.assertIn("0x4AAAAAAtest", html)
                self.assertIn("onCaptchaSuccess", html)

            # POST solution to submit endpoint
            submit_url = f"http://127.0.0.1:{loopback.port}/captcha/{c.id}/submit"
            key = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["k"][0]
            forged = urllib.request.Request(submit_url, data=json.dumps({"token": "x", "key": "guess"}).encode(),
                                            headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(forged, timeout=5) as forged_resp:
                self.assertEqual(json.loads(forged_resp.read())["outcome"], "denied", "a guessed key is refused")
            post_data = json.dumps({"token": "solved-turnstile-token-abc", "key": key}).encode("utf-8")
            post_req = urllib.request.Request(submit_url, data=post_data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(post_req, timeout=5) as post_resp:
                self.assertEqual(post_resp.status, 200)
                body = json.loads(post_resp.read().decode("utf-8"))
                self.assertTrue(body["success"])

            replay = urllib.request.Request(submit_url, data=post_data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(replay, timeout=5) as replay_resp:
                self.assertFalse(json.loads(replay_resp.read())["success"], "a key opens one answer only")
            # An answer is received, not yet solved: the site's verdict settles it.
            solved_c = self.manager.get_challenge(c.id)
            self.assertEqual(solved_c.status, CaptchaStatus.VERIFYING)
            self.assertEqual(solved_c.solution, {"token": "solved-turnstile-token-abc"})
            self.assertEqual(solved_c.solver_used, "browser_loopback")
        finally:
            loopback.stop()

    def test_single_dialog_flood_control_queue(self) -> None:
        # Disable auto solvers so all fall through to interactive UI
        for s in self.manager.solvers:
            if s.solver_id != "interactive_ui":
                s.enabled = False

        c1 = CaptchaChallenge(id="c1", provider_id="host1", captcha_type=CaptchaType.IMAGE_TEXT, timeout_seconds=30)
        c2 = CaptchaChallenge(id="c2", provider_id="host2", captcha_type=CaptchaType.IMAGE_TEXT, timeout_seconds=30)
        c3 = CaptchaChallenge(id="c3", provider_id="host3", captcha_type=CaptchaType.IMAGE_TEXT, timeout_seconds=30)

        loop = asyncio.new_event_loop()
        try:
            # Launch all 3 concurrently
            f1 = loop.create_task(self.manager.request_solution(c1))
            f2 = loop.create_task(self.manager.request_solution(c2))
            f3 = loop.create_task(self.manager.request_solution(c3))

            # Run loop briefly to let enqueue happen
            loop.run_until_complete(asyncio.sleep(0.05))

            # Only c1 should be active in UI
            active = self.manager.get_active_ui_challenge()
            self.assertIsNotNone(active)
            self.assertEqual(active["id"], "c1")
            self.assertEqual(self.manager._ui_queue, ["c2", "c3"])

            # Solve c1
            self.manager.solve_challenge("c1", {"text": "sol1"})
            loop.run_until_complete(asyncio.sleep(0.05))
            self.assertEqual(f1.result(), {"text": "sol1"})

            # c2 should now be promoted as active in UI
            active = self.manager.get_active_ui_challenge()
            self.assertEqual(active["id"], "c2")
            self.assertEqual(self.manager._ui_queue, ["c3"])

            # Skip c2
            self.manager.skip_challenge("c2", scope="single")
            loop.run_until_complete(asyncio.sleep(0.05))
            self.assertTrue(f2.done())
            with self.assertRaises(Exception):
                f2.result()

            # c3 should now be promoted
            active = self.manager.get_active_ui_challenge()
            self.assertEqual(active["id"], "c3")
            self.assertEqual(self.manager._ui_queue, [])

            # Solve c3
            self.manager.solve_challenge("c3", {"text": "sol3"})
            loop.run_until_complete(asyncio.sleep(0.05))
            self.assertEqual(f3.result(), {"text": "sol3"})

            # Queue empty
            self.assertIsNone(self.manager.get_active_ui_challenge())
        finally:
            loop.close()

    def test_forced_cascade_tries_clearcote_even_when_master_switch_is_off(self) -> None:
        class _ClearcoteFixture:
            solver_id = "automated_browser"
            name = "Clearcote fixture"
            enabled = True
            priority = 1

            def capability(self):
                from engine.challenge_routing import PAGE_INPUTS, SolverCapability
                return SolverCapability(frozenset({"turnstile"}), "clearance", "reusable_clearance",
                                        inputs={"*": (PAGE_INPUTS,)}, identity_bound=True)

            def health(self):
                from engine.challenge_routing import Health
                return Health(True)

            def can_solve(self, challenge):
                return challenge.captcha_type == CaptchaType.TURNSTILE

            async def solve(self, challenge):
                return {"token": "fixture-token", "cookies": {"cf_clearance": "reusable"}}

        self.manager.solvers = [_ClearcoteFixture()]
        challenge = CaptchaChallenge(
            id="forced-cascade-1",
            task_id="task-forced-cascade",
            provider_id="datanodes.to",
            captcha_type=CaptchaType.TURNSTILE,
            params={"page_url": "https://datanodes.to/file/1", "group_id": "package-1"},
        )
        self.store.save(DownloadTask(id="task-forced-cascade", source_url="https://datanodes.to/file/1", destination="downloads"))

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(self.manager.request_solution(challenge, force_automated=True))
        finally:
            loop.close()

        self.assertEqual(result["token"], "fixture-token")
        self.assertEqual(challenge.status, CaptchaStatus.VERIFYING)
        self.assertEqual(challenge.solver_used, "automated_browser")
        state_events = [
            event for event in self.store.events_since()
            if event["event_type"] == "CaptchaStateChanged"
        ]
        self.assertTrue(any(event["payload"].get("coordinator_state") == "clearcote_pending" for event in state_events))
        self.assertTrue(any(event["payload"].get("coordinator_state") == "clearcote_active" for event in state_events))

    def test_host_skip_policy(self) -> None:
        c1 = CaptchaChallenge(id="ch1", provider_id="annoyinghost.com", captcha_type=CaptchaType.IMAGE_TEXT)
        self.manager._challenges[c1.id] = c1
        self.manager.skip_challenge("ch1", scope="host", duration_seconds=60)

        # Immediate next request for annoyinghost.com should auto-skip
        c2 = CaptchaChallenge(id="ch2", provider_id="annoyinghost.com", captcha_type=CaptchaType.IMAGE_TEXT)
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaises(TimeoutError):
                loop.run_until_complete(self.manager.request_solution(c2))
            self.assertEqual(c2.status, CaptchaStatus.SKIPPED)
        finally:
            loop.close()

    def test_database_persistence(self) -> None:
        task = DownloadTask(id="task-xyz", source_url="https://fileboom.me/test", destination="downloads")
        self.store.save(task)

        challenge = CaptchaChallenge(
            id="persist-ch-1",
            task_id="task-xyz",
            provider_id="fileboom",
            captcha_type=CaptchaType.POSITIONAL_CLICK,
            params={"image_url": "https://fileboom.me/captcha.jpg", "prompt": "Click puzzle center"},
            timeout_seconds=120.0,
        )
        self.store.save_captcha_challenge(challenge)

        # Retrieve
        loaded = self.store.get_captcha_challenge("persist-ch-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.id, "persist-ch-1")
        self.assertEqual(loaded.provider_id, "fileboom")
        self.assertEqual(loaded.captcha_type, CaptchaType.POSITIONAL_CLICK)
        self.assertEqual(loaded.params["prompt"], "Click puzzle center")
        self.assertEqual(loaded.status, CaptchaStatus.PENDING)

        # Update status to solved
        challenge.status = CaptchaStatus.SOLVED
        challenge.solution = {"coordinates": [{"x": 100, "y": 150}]}
        challenge.solver_used = "interactive_ui"
        self.store.save_captcha_challenge(challenge)

        updated = self.store.get_captcha_challenge("persist-ch-1")
        self.assertEqual(updated.status, CaptchaStatus.SOLVED)
        self.assertEqual(updated.solution["coordinates"][0]["x"], 100)

        # List challenges
        all_challenges = self.store.list_captcha_challenges("task-xyz")
        self.assertEqual(len(all_challenges), 1)
        self.assertEqual(all_challenges[0]["id"], "persist-ch-1")

    def test_service_jsonrpc_captcha_endpoints(self) -> None:
        service = EngineService(self.data_dir / "service_data")
        try:
            # 1. Config endpoint
            cfg = service.dispatch("captcha_get_config")
            self.assertIn("solvers", cfg)
            self.assertTrue(any(s["id"] == "local_ocr" for s in cfg["solvers"]))

            # 2. Update config
            service.dispatch("captcha_set_config", {
                "solvers": [{"id": "twocaptcha", "api_key": "testkey123", "enabled": True}]
            })
            updated_cfg = service.dispatch("captcha_get_config")
            two = next(s for s in updated_cfg["solvers"] if s["id"] == "twocaptcha")
            self.assertTrue(two["enabled"])
            self.assertTrue(two["has_key"])

            # 3. Create a challenge and retrieve via JSON-RPC
            parent_task = DownloadTask(id="task-rpc", source_url="https://k2s.cc/file.bin", destination="downloads")
            service.store.save(parent_task)

            challenge = CaptchaChallenge(
                id="rpc-ch-1",
                task_id="task-rpc",
                provider_id="keep2share",
                captcha_type=CaptchaType.IMAGE_TEXT,
                params={"image_url": "https://k2s.cc/captcha.png"},
            )
            service.captcha._challenges[challenge.id] = challenge
            service.store.save_captcha_challenge(challenge)

            got = service.dispatch("captcha_get_challenge", {"challenge_id": "rpc-ch-1"})
            self.assertEqual(got["id"], "rpc-ch-1")
            self.assertEqual(got["provider_id"], "keep2share")

            # 4. Solve challenge via JSON-RPC
            solve_res = service.dispatch("captcha_solve", {
                "challenge_id": "rpc-ch-1",
                "solution": {"text": "73921"},
            })
            self.assertTrue(solve_res["success"])
            self.assertEqual(service.captcha.get_challenge("rpc-ch-1").status, CaptchaStatus.VERIFYING)

            # 5. Report result feedback
            rep = service.dispatch("captcha_report_result", {
                "challenge_id": "rpc-ch-1",
                "valid": True,
            })
            self.assertTrue(rep["reported"])

            # 6. Loopback URL endpoint
            loop_url = service.dispatch("captcha_loopback_url", {"challenge_id": "rpc-ch-1"})
            self.assertIn("/captcha/rpc-ch-1", loop_url["url"])
        finally:
            service.close()

    def test_task_resumption_after_captcha(self) -> None:
        service = EngineService(self.data_dir / "service_data_resume")
        try:
            # Create a task in needs_user state waiting for captcha
            task = DownloadTask(id="task-needs-cap", source_url="https://rapidgator.net/file/1", destination="downloads")
            task.state = "needs_user"
            task.user_action = "captcha"
            task.user_challenge = {"challenge_id": "cap-resume-1", "captcha_type": "turnstile"}
            service.store.save(task)

            # Create the corresponding challenge in manager
            challenge = CaptchaChallenge(
                id="cap-resume-1",
                task_id=task.id,
                provider_id="rapidgator",
                captcha_type=CaptchaType.TURNSTILE,
                params={"site_key": "rg-key", "page_url": "https://rapidgator.net/file/1"},
            )
            service.captcha._challenges[challenge.id] = challenge
            service.store.save_captcha_challenge(challenge)

            # Solve via captcha_solve RPC
            res = service.dispatch("captcha_solve", {
                "challenge_id": "cap-resume-1",
                "solution": {"token": "turnstile-solution-token-abc"},
            })
            self.assertTrue(res["success"])

            # Answered, and on its way to the site with the resumed task.
            self.assertEqual(service.captcha.get_challenge("cap-resume-1").status, CaptchaStatus.VERIFYING)
            self.assertTrue(service.store.get("task-needs-cap").user_challenge.get("verifying"))

            # Verify task was automatically resumed (transitioned to queued or resolving for execution)
            resumed_task = service.store.get("task-needs-cap")
            self.assertIn(resumed_task.state, {"queued", "resolving"})
            self.assertIsNone(resumed_task.user_action)
        finally:
            service.close()

    def test_automated_captcha_solve_starts_and_emits_task_update(self) -> None:
        service = EngineService(self.data_dir / "service_data_auto_solve")
        try:
            task = DownloadTask(id="task-auto-cap", source_url="https://rapidgator.net/file/2", destination="downloads")
            task.state = "needs_user"
            service.store.save(task)

            challenge = CaptchaChallenge(
                id="auto-cap-1",
                task_id=task.id,
                provider_id="rapidgator",
                captcha_type=CaptchaType.TURNSTILE,
                params={"page_url": task.source_url},
            )
            service.captcha._challenges[challenge.id] = challenge
            service.store.save_captcha_challenge(challenge)

            # The click path should acknowledge the request and launch the worker;
            # do not exercise an external browser in this unit test.
            with patch("engine.browser_solver.solver_daemon.solve_challenge_sync", return_value={"success": False}):
                result = service.dispatch("captcha_solve", {
                    "challenge_id": challenge.id,
                    "solver_id": "clearcote",
                })

            self.assertTrue(result["success"])
            self.assertTrue(result["started"])
            deadline = time.time() + 2
            while time.time() < deadline:
                current = service.store.get(task.id)
                if current and current.user_challenge.get("solver_error"):
                    break
                time.sleep(0.01)
            current = service.store.get(task.id)
            self.assertFalse(current.user_challenge["solver_active"])
            self.assertIn("solver_error", current.user_challenge)
            pending = service.events.pending()
            self.assertTrue(any(event["event_type"] == "TaskUpdated" and event["task_id"] == task.id for event in pending))
        finally:
            service.close()

    def test_visible_solver_promotion_disables_short_auto_skip(self) -> None:
        """A promoted visible solver must outlive the UI auto-skip window."""
        self.store.set_setting("captcha_ui_preferences", {
            "captchaAutoSkip": True,
            "captchaAutoSkipSecs": 1,
        })
        self.store.save(DownloadTask(
            id="task-visible-promotion-1",
            source_url="https://datanodes.to/file",
            destination="downloads",
        ))
        challenge = CaptchaChallenge(
            id="visible-promotion-window-1",
            task_id="task-visible-promotion-1",
            provider_id="datanodes.to",
            captcha_type=CaptchaType.TURNSTILE,
            params={"page_url": "https://datanodes.to/file"},
            timeout_seconds=90.0,
        )
        self.manager.register_challenge(challenge)

        async def exercise() -> dict:
            waiting = asyncio.create_task(self.manager.enqueue_for_ui(challenge))
            await asyncio.sleep(0.2)
            challenge.params["manual_solver_active"] = True
            challenge.expires_at = time.time() + 180.0
            await asyncio.sleep(1.0)
            self.assertFalse(waiting.done(), "visible solver was incorrectly auto-skipped")
            self.assertTrue(self.manager.solve_challenge(
                challenge.id,
                {"turnstile_token": "visible-token"},
                "clearcote",
            ))
            return await waiting

        result = asyncio.run(exercise())
        self.assertEqual(result["turnstile_token"], "visible-token")

    def test_automated_captcha_solver_failure_is_visible(self) -> None:
        service = EngineService(self.data_dir / "service_data_auto_solve_failure")
        try:
            task = DownloadTask(id="task-auto-cap-failure", source_url="https://rapidgator.net/file/3", destination="downloads")
            task.state = "needs_user"
            task.user_action = "turnstile"
            service.store.save(task)
            challenge = CaptchaChallenge(
                id="auto-cap-failure-1",
                task_id=task.id,
                provider_id="rapidgator",
                captcha_type=CaptchaType.TURNSTILE,
                params={"page_url": task.source_url},
            )
            service.captcha._challenges[challenge.id] = challenge
            service.store.save_captcha_challenge(challenge)

            with patch("engine.browser_solver.solver_daemon.solve_challenge_sync", return_value={"success": False, "error": "browser unavailable"}):
                result = service.dispatch("captcha_solve", {"challenge_id": challenge.id, "solver_id": "clearcote"})

            self.assertTrue(result["started"])
            deadline = time.time() + 2
            while time.time() < deadline:
                current = service.store.get(task.id)
                if current and current.user_challenge.get("solver_error"):
                    break
                time.sleep(0.01)
            current = service.store.get(task.id)
            self.assertFalse(current.user_challenge["solver_active"])
            self.assertEqual(current.user_challenge["solver_error"], "browser unavailable")
        finally:
            service.close()

    def test_token_solution_is_required_and_redacted(self) -> None:
        challenge = CaptchaChallenge(
            id="token-validation-1",
            provider_id="cloudflare",
            captcha_type=CaptchaType.TURNSTILE,
        )
        self.manager._challenges[challenge.id] = challenge

        self.assertFalse(self.manager.solve_challenge(challenge.id, {"cookies": {"cf_clearance": "secret"}}))
        self.assertEqual(challenge.status, CaptchaStatus.PENDING)
        self.assertEqual(challenge.error, "CAPTCHA solver returned no token")

        secret = "turnstile-secret-token"
        self.assertTrue(self.manager.solve_challenge(
            challenge.id,
            {"token": secret, "cookies": {"session": "cookie-secret"}, "method": "mock"},
        ))
        self.assertEqual(challenge.solution["token"], secret)
        public = challenge.to_dict()
        serialized = json.dumps(public)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("cookie-secret", serialized)
        self.assertNotIn("token", public["solution"])
        self.assertEqual(public["solution"]["method"], "mock")
        persisted = self.store.get_captcha_challenge(challenge.id)
        self.assertIsNotNone(persisted)
        self.assertNotIn(secret, json.dumps(persisted.to_dict()))


if __name__ == "__main__":
    unittest.main()
