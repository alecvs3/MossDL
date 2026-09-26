import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from engine.captcha import (
    CaptchaChallenge,
    CaptchaManager,
    CaptchaStatus,
    CaptchaType,
    FlareSolverrSolver,
)
from engine.providers.cyberdrop_hosts import VikingfileProvider
from engine.errors import NeedsUser


class TestVikingfileTurnstileAndFlareSolverrFixes(unittest.TestCase):
    def test_flaresolverr_returns_none_when_no_token(self):
        solver = FlareSolverrSolver()
        challenge = CaptchaChallenge(
            id="test-challenge-1",
            captcha_type=CaptchaType.TURNSTILE,
            params={"url": "https://vikingfile.com/f/A94TpVDc66"},
            timeout_seconds=30.0,
        )

        mock_resolution = MagicMock()
        mock_resolution.status = "ok"
        mock_resolution.solution_cookies = [{"name": "theme", "value": "dark"}]
        mock_resolution.cf_clearance = None
        mock_resolution.turnstile_token = None
        mock_resolution.response_html = "<html><body>No turnstile token value here</body></html>"
        mock_resolution.user_agent = "Mozilla/5.0"

        with patch.object(solver.client, "resolve", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = mock_resolution
            result = asyncio.run(solver.solve(challenge))
            self.assertIsNone(result, "Solver must return None when no authentic token exists, not 'flaresolverr_ok'")

    def test_flaresolverr_returns_solution_when_turnstile_token_present(self):
        solver = FlareSolverrSolver()
        challenge = CaptchaChallenge(
            id="test-challenge-2",
            captcha_type=CaptchaType.TURNSTILE,
            params={"url": "https://vikingfile.com/f/A94TpVDc66"},
            timeout_seconds=30.0,
        )

        mock_resolution = MagicMock()
        mock_resolution.status = "ok"
        mock_resolution.solution_cookies = []
        mock_resolution.cf_clearance = None
        mock_resolution.turnstile_token = "valid_real_cf_token_123"
        mock_resolution.response_html = "<html></html>"
        mock_resolution.user_agent = "Mozilla/5.0"

        with patch.object(solver.client, "resolve", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = mock_resolution
            result = asyncio.run(solver.solve(challenge))
            self.assertIsNotNone(result)
            self.assertEqual(result.get("token"), "valid_real_cf_token_123")

    def test_vikingfile_accepts_direct_link_solution(self):
        direct_url = "https://vikingfile.04b3d96d52475741e6b10f97f0a84a16.r2.cloudflarestorage.com/download.zip"
        landing = (
            b'<span id="filename">download.zip</span><span id="size">1 MB</span>',
            "https://vikingfile.com/f/A94TpVDc66",
            {},
        )
        # This is a unit test for solution-shape handling. A live landing request
        # made the fast suite spend 50+ seconds waiting on vikingfile.com and
        # fail whenever the provider was unavailable.
        with patch("engine.providers.cyberdrop_hosts._fetch", return_value=landing):
            res = VikingfileProvider.resolve(
                "https://vikingfile.com/f/A94TpVDc66",
                secrets={"turnstile_token": direct_url},
            )
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].direct_url, direct_url)

            res2 = VikingfileProvider.resolve(
                "https://vikingfile.com/f/A94TpVDc66",
                secrets={"captcha_solution": {"link": direct_url}},
            )
            self.assertEqual(len(res2), 1)
            self.assertEqual(res2[0].direct_url, direct_url)

    def test_cascade_falls_back_to_pending_ui(self):
        manager = CaptchaManager()
        for s in manager.solvers:
            if s.solver_id != "interactive_ui":
                s.enabled = False

        challenge = CaptchaChallenge(
            id="test-challenge-ui",
            captcha_type=CaptchaType.TURNSTILE,
            params={"url": "https://vikingfile.com/f/A94TpVDc66"},
            timeout_seconds=30.0,
        )

        async def run_test():
            req_task = asyncio.create_task(manager.request_solution(challenge))
            await asyncio.sleep(0.05)
            self.assertEqual(challenge.status, CaptchaStatus.PENDING)
            active = manager.list_pending_challenges()
            self.assertTrue(any(c["id"] == "test-challenge-ui" for c in active))
            manager.solve_challenge("test-challenge-ui", {"token": "user_solved_token_xyz"})
            sol = await req_task
            self.assertEqual(sol.get("token"), "user_solved_token_xyz")

        asyncio.run(run_test())

    def test_vikingfile_auto_solve_returns_direct_url(self):
        direct_url = "https://vikingfile.com/d/z3VdG5gj7T/file.rar"
        fake_html = b"""<html><head><title>[Ryuugames] test</title></head>
        <body><span id="filename">test.rar</span><span id="size">100 MB</span>
        <div class="cf-turnstile" data-sitekey="0x4AAAAAAAgbsMNBuk2d3Qp6"></div>
        </body></html>"""
        with patch("engine.providers.cyberdrop_hosts._fetch", return_value=(fake_html, "https://vikingfile.com/f/A94TpVDc66", 200)), \
             patch("engine.browser_solver.solver_daemon.solve_challenge_sync", return_value={"success": True, "direct_url": direct_url, "engine": "clearcote", "cookies": {"cf_clearance": "cleared_123"}}):
            items = VikingfileProvider.resolve("https://vikingfile.com/f/A94TpVDc66")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].direct_url, direct_url)
            self.assertEqual(items[0].display_name, "test.rar")
            self.assertEqual(items[0].cookies.get("cf_clearance"), "cleared_123")

    def test_vikingfile_auto_solve_returns_token_and_posts(self):
        direct_url = "https://vikingfile.com/d/z3VdG5gj7T/file.rar"
        fake_html = b"""<html><head><title>[Ryuugames] test</title></head>
        <body><span id="filename">test.rar</span><span id="size">100 MB</span>
        <div class="cf-turnstile" data-sitekey="0x4AAAAAAAgbsMNBuk2d3Qp6"></div>
        </body></html>"""
        with patch("engine.providers.cyberdrop_hosts._fetch", return_value=(fake_html, "https://vikingfile.com/f/A94TpVDc66", 200)), \
             patch("engine.browser_solver.solver_daemon.solve_challenge_sync", return_value={"success": True, "turnstile_token": "tok_auto_123", "engine": "clearcote"}), \
             patch("engine.providers.cyberdrop_hosts._post_json", return_value={"link": direct_url}):
            items = VikingfileProvider.resolve("https://vikingfile.com/f/A94TpVDc66")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].direct_url, direct_url)

    def test_vikingfile_auto_solve_failure_raises_needs_user(self):
        fake_html = b"""<html><head><title>[Ryuugames] test</title></head>
        <body><span id="filename">test.rar</span><span id="size">100 MB</span>
        <div class="cf-turnstile" data-sitekey="0x4AAAAAAAgbsMNBuk2d3Qp6"></div>
        </body></html>"""
        with patch("engine.providers.cyberdrop_hosts._fetch", return_value=(fake_html, "https://vikingfile.com/f/A94TpVDc66", 200)), \
             patch("engine.browser_solver.solver_daemon.solve_challenge_sync", return_value={"success": False, "error": "timeout", "engine": "clearcote"}):
            with self.assertRaises(NeedsUser) as ctx:
                VikingfileProvider.resolve("https://vikingfile.com/f/A94TpVDc66")
            self.assertEqual(ctx.exception.action, "turnstile")

    def test_datanodes_countdown_telemetry_loop(self):
        from engine.providers.cyberdrop_hosts import DatanodesProvider
        from engine.timer_scheduler import timer_scheduler
        fake_html = """<html><body>
        <div rand="rand123" dl-token="tok456" :countdown="3"></div>
        </body></html>"""
        recorded_messages = []
        self.addCleanup(timer_scheduler.clear_host, "datanodes.to")
        with patch("engine.http_client.get") as mock_get, \
             patch.object(timer_scheduler, "wait_for_sync") as mock_wait, \
             patch("engine.providers.cyberdrop_hosts.DatanodesProvider._item") as mock_item, \
             patch("engine.telemetry.telemetry_bus.record") as mock_record:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = fake_html.encode("utf-8")
            mock_resp.geturl.return_value = "https://datanodes.to/abc1/file.rar"
            mock_resp.headers = {}
            mock_get.return_value = mock_resp

            def record_capture(*args, **kwargs):
                if "message" in kwargs:
                    recorded_messages.append(kwargs["message"])
            mock_record.side_effect = record_capture

            with patch("engine.http_client.post") as mock_post:
                mock_post_resp = MagicMock()
                mock_post_resp.status = 200
                mock_post_resp.read.return_value = b'{"url": "https://datanodes.to/d/file.rar"}'
                mock_post_resp.geturl.return_value = "https://datanodes.to/download"
                mock_post_resp.headers = {}
                mock_post.return_value = mock_post_resp
                mock_item.return_value = MagicMock()

                DatanodesProvider.resolve("https://datanodes.to/abc1/file.rar", secrets={"task_id": "testtask123"})
                combined = "\n".join(recorded_messages)
                self.assertIn("[TIMER_CANDIDATES]", combined)
                self.assertIn("[TIMER_ARMED]", combined)
                self.assertTrue(mock_wait.called)
                self.assertFalse(any("Waiting countdown" in m for m in recorded_messages))

    def test_datanodes_maps_post_browser_challenge_to_manual_action(self):
        from engine.providers.cyberdrop_hosts import DatanodesProvider

        probe = MagicMock()
        probe.status = 200
        probe.read.return_value = b'<div rand="r" dl-token="d" class="cf-turnstile"></div>'
        probe.geturl.return_value = "https://datanodes.to/challenge/file.rar"
        probe.headers = {}
        post = MagicMock()
        post.status = 200
        post.read.return_value = b'<meta name="bm-site-verification" content="challenge">'
        post.geturl.return_value = "https://datanodes.to/download"
        post.headers = {}

        with patch("engine.http_client.get", return_value=probe), \
             patch("engine.http_client.post", return_value=post), \
             patch("engine.browser_solver.solver_daemon.solve_challenge_sync", return_value={
                 "success": True, "turnstile_token": "synthetic-token", "cookies": {},
             }):
            with self.assertRaises(NeedsUser) as ctx:
                DatanodesProvider.resolve("https://datanodes.to/challenge/file.rar")
        self.assertEqual(ctx.exception.action, "turnstile")
        self.assertEqual(ctx.exception.challenge.get("response_reason"), "provider_challenge_after_token")

    def test_multipart_member_defers_inline_solver_to_package_coordinator(self):
        from engine.providers.cyberdrop_hosts import DatanodesProvider

        probe = MagicMock()
        probe.status = 200
        probe.read.return_value = b'<div rand="r" dl-token="d" class="cf-turnstile"></div>'
        probe.geturl.return_value = "https://datanodes.to/challenge/file.part2.rar"
        probe.headers = {}

        with patch("engine.http_client.get", return_value=probe), \
             patch("engine.http_client.post") as mock_post, \
             patch("engine.browser_solver.solver_daemon.solve_challenge_sync") as mock_solve:
            with self.assertRaises(NeedsUser) as ctx:
                DatanodesProvider.resolve(
                    "https://datanodes.to/challenge/file.part2.rar",
                    secrets={"task_id": "t-multipart", "multipart_package": "datanodes.to:file"},
                )
        self.assertEqual(ctx.exception.action, "turnstile")
        self.assertFalse(mock_solve.called, "package members must not run an inline browser solve")
        self.assertFalse(mock_post.called, "package members must not POST without package clearance")


if __name__ == "__main__":
    unittest.main()
