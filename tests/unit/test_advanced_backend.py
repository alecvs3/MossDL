from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engine.archive_joiner import ArchiveOrchestrator, BinaryPartJoiner, MultiPartDetector, MultiPartInfo
from engine.captcha import CaptchaChallenge, CaptchaManager, CaptchaStatus, CaptchaType
from engine.darknet_solver import DarknetYoloSolver
from engine.routes import RouteManager
from engine.service import EngineService
from engine.shortlink_resolver import HopResult, RecursiveShortlinkResolver


class AdvancedBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.work_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # =========================================================================
    # 1. Multi-Part Archive Combining Tests
    # =========================================================================

    def test_detect_various_multipart_naming_schemes(self) -> None:
        # Standard .partX.ext
        info1 = MultiPartDetector.detect("Game.Setup.part01.rar")
        self.assertIsNotNone(info1)
        self.assertEqual(info1.base_name, "Game.Setup")
        self.assertEqual(info1.part_number, 1)
        self.assertEqual(info1.format_type, "part_archive")
        self.assertEqual(info1.extension, "rar")

        info2 = MultiPartDetector.detect("Package.part3.7z")
        self.assertIsNotNone(info2)
        self.assertEqual(info2.part_number, 3)
        self.assertEqual(info2.extension, "7z")

        # Split binary .001
        info3 = MultiPartDetector.detect("Movie.4k.iso.002")
        self.assertIsNotNone(info3)
        self.assertEqual(info3.base_name, "Movie.4k.iso")
        self.assertEqual(info3.part_number, 2)
        self.assertEqual(info3.format_type, "split_binary")

        # Legacy RAR .r00, .r01
        info4 = MultiPartDetector.detect("SceneRelease.r00")
        self.assertIsNotNone(info4)
        self.assertEqual(info4.part_number, 2)
        self.assertEqual(info4.format_type, "numbered_rar")

        # Multi-part ZIP .z01
        info5 = MultiPartDetector.detect("Photos.z01")
        self.assertIsNotNone(info5)
        self.assertEqual(info5.part_number, 1)
        self.assertEqual(info5.format_type, "multipart_zip")

        # Regular non-split file
        self.assertIsNone(MultiPartDetector.detect("document.pdf"))
        self.assertIsNone(MultiPartDetector.detect("song.mp3"))

    def test_binary_part_joiner_integrity(self) -> None:
        # Create a test payload of 256 KB
        original_data = b"TRANSFER_ENGINE_MULTIPART_TEST_" * 8192
        expected_sha256 = hashlib.sha256(original_data).hexdigest()

        # Split into 3 arbitrary chunks
        chunk1 = original_data[:70000]
        chunk2 = original_data[70000:180000]
        chunk3 = original_data[180000:]

        p1 = self.work_dir / "Data.bin.001"
        p2 = self.work_dir / "Data.bin.002"
        p3 = self.work_dir / "Data.bin.003"
        p1.write_bytes(chunk1)
        p2.write_bytes(chunk2)
        p3.write_bytes(chunk3)

        out_file = self.work_dir / "Data.bin"
        joined_path, digest, total_size = BinaryPartJoiner.join([p1, p2, p3], out_file)

        self.assertTrue(out_file.is_file())
        self.assertEqual(total_size, len(original_data))
        self.assertEqual(digest, expected_sha256)
        self.assertEqual(out_file.read_bytes(), original_data)

    def test_package_completeness_and_missing_volume_detection(self) -> None:
        # Create parts 1 and 3 (part 2 is missing!)
        (self.work_dir / "Game.part1.rar").write_text("chunk 1")
        (self.work_dir / "Game.part3.rar").write_text("chunk 3")

        complete, parts = MultiPartDetector.is_package_complete(self.work_dir, "Game", "part_archive")
        self.assertFalse(complete)
        self.assertEqual(len(parts), 2)

        # Add part 2
        (self.work_dir / "Game.part2.rar").write_text("chunk 2")
        complete, parts = MultiPartDetector.is_package_complete(self.work_dir, "Game", "part_archive")
        self.assertTrue(complete)
        self.assertEqual(len(parts), 3)

    def test_archive_orchestrator_process_package(self) -> None:
        orchestrator = ArchiveOrchestrator()

        # Create split files: Payload.dat.001, Payload.dat.002
        data_a = b"First Half of File. "
        data_b = b"Second Half of File."
        p1 = self.work_dir / "Payload.dat.001"
        p2 = self.work_dir / "Payload.dat.002"
        p1.write_bytes(data_a)
        p2.write_bytes(data_b)

        # Process package with delete_parts_on_success=True
        res = orchestrator.process_package(
            directory=self.work_dir,
            base_name="Payload.dat",
            format_type="split_binary",
            delete_parts_on_success=True,
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["action"], "joined_binary")
        joined_file = Path(res["output_file"])
        self.assertTrue(joined_file.is_file())
        self.assertEqual(joined_file.read_bytes(), data_a + data_b)
        self.assertTrue(res["parts_removed"])
        self.assertFalse(p1.exists())
        self.assertFalse(p2.exists())

    # =========================================================================
    # 2. Recursive Multi-Hop Shortlink Bypasser Tests
    # =========================================================================

    def test_query_param_nested_base64_unwrap(self) -> None:
        resolver = RecursiveShortlinkResolver()

        # Plain ?url=
        plain = "https://short.link/go?url=https://mediafire.com/file/xyz"
        unwrapped1 = resolver._extract_embedded_param_url(plain)
        self.assertEqual(unwrapped1, "https://mediafire.com/file/xyz")

        # Base64 encoded target ?dest=
        target = "https://rapidgator.net/file/998877/game.rar"
        b64 = "https://bypass.me/out?dest=aHR0cHM6Ly9yYXBpZGdhdG9yLm5ldC9maWxlLzk5ODg3Ny9nYW1lLnJhcg=="
        unwrapped2 = resolver._extract_embedded_param_url(b64)
        self.assertEqual(unwrapped2, target)

    def test_recursive_chain_3_hops_with_simulated_timer(self) -> None:
        resolver = RecursiveShortlinkResolver()

        # Mock fetcher simulating 3 hops:
        # Hop 1: https://link1.test -> 302 to https://link2.test/wait
        # Hop 2: https://link2.test/wait -> HTML with 1s delay and meta-refresh to https://mediafire.com/file/final.zip
        # Hop 3: https://mediafire.com/file/final.zip (Terminal host)
        def mock_fetch(url: str):
            if "link1.test" in url:
                return 302, "text/html", {"Location": "https://link2.test/wait"}, "redirecting..."
            if "link2.test/wait" in url:
                html = """
                <html>
                <head>
                  <meta http-equiv="refresh" content="1; url=https://mediafire.com/file/final.zip">
                </head>
                <body><div data-delay="1">Please wait 1 seconds</div></body>
                </html>
                """
                return 200, "text/html", {}, html
            if "mediafire.com" in url:
                return 200, "application/octet-stream", {}, "binary content"
            return 404, "text/plain", {}, "not found"

        resolver.http_fetcher = mock_fetch

        loop = asyncio.new_event_loop()
        try:
            final_url, hops = loop.run_until_complete(
                resolver.resolve_chain("https://link1.test/start")
            )
            self.assertEqual(final_url, "https://mediafire.com/file/final.zip")
            self.assertEqual(len(hops), 2)
            self.assertEqual(hops[0].strategy, "http_redirect")
            self.assertEqual(hops[1].strategy, "meta_refresh")
        finally:
            loop.close()

    def test_recursive_chain_loop_detection(self) -> None:
        resolver = RecursiveShortlinkResolver(max_hops=5)

        # Circular redirect: A -> B -> A
        def mock_loop_fetch(url: str):
            if "loopA.test" in url:
                return 302, "text/html", {"Location": "https://loopB.test/step"}, ""
            if "loopB.test" in url:
                return 302, "text/html", {"Location": "https://loopA.test/step"}, ""
            return 200, "text/html", {}, ""

        resolver.http_fetcher = mock_loop_fetch

        loop = asyncio.new_event_loop()
        try:
            final_url, hops = loop.run_until_complete(
                resolver.resolve_chain("https://loopA.test/step")
            )
            # Must catch loop and abort cleanly
            self.assertTrue(any(h.status == "loop_detected" for h in hops))
        finally:
            loop.close()

    def test_recursive_chain_with_captcha_interruption(self) -> None:
        captcha_mgr = CaptchaManager()
        # Wire mock solver for Turnstile
        for s in captcha_mgr.solvers:
            s.enabled = False

        from engine.captcha import CaptchaSolver
        from engine.challenge_routing import SolverCapability

        class MockTurnstileSolver(CaptchaSolver):
            def capability(self):
                return SolverCapability(frozenset({"turnstile"}), "token", "single_use_token")
            def can_solve(self, ch):
                return ch.captcha_type == CaptchaType.TURNSTILE
            async def solve(self, ch):
                return {"token": "cf-solved-token-xyz"}

        captcha_mgr.solvers.insert(0, MockTurnstileSolver("mock_turnstile", "Mock Turnstile"))

        resolver = RecursiveShortlinkResolver(captcha_manager=captcha_mgr)

        # Hop 1: Returns HTML containing Cloudflare Turnstile challenge
        # Form postback submits token and redirects to https://gofile.io/d/final123
        def mock_captcha_fetch(url: str):
            if "cf-protected.test" in url:
                html = """
                <html>
                <body>
                  <form action="https://cf-protected.test/verify" method="POST">
                    <input type="hidden" name="ref" value="12345" />
                    <div class="cf-turnstile" data-sitekey="0x4AAAAAAtest"></div>
                  </form>
                </body>
                </html>
                """
                return 200, "text/html", {}, html
            return 200, "text/html", {}, ""

        resolver.http_fetcher = mock_captcha_fetch

        # Mock submit postback
        async def mock_submit(url, html, token):
            return "https://gofile.io/d/final123"

        resolver._submit_captcha_token = mock_submit

        loop = asyncio.new_event_loop()
        try:
            final_url, hops = loop.run_until_complete(
                resolver.resolve_chain("https://cf-protected.test/download")
            )
            self.assertEqual(final_url, "https://gofile.io/d/final123")
            self.assertTrue(any(h.captcha_encountered for h in hops))
        finally:
            loop.close()

    # =========================================================================
    # 3. Native Darknet/YOLO Offline Captcha Solver Tests
    # =========================================================================

    def test_darknet_solver_can_solve_supported_hosts(self) -> None:
        solver = DarknetYoloSolver()
        c_k2s = CaptchaChallenge(provider_id="keep2share.cc", captcha_type=CaptchaType.IMAGE_TEXT)
        self.assertTrue(solver.can_solve(c_k2s))

        c_fj = CaptchaChallenge(provider_id="filejoker.net", captcha_type=CaptchaType.IMAGE_TEXT)
        self.assertTrue(solver.can_solve(c_fj))

        c_turnstile = CaptchaChallenge(provider_id="keep2share.cc", captcha_type=CaptchaType.TURNSTILE)
        self.assertFalse(solver.can_solve(c_turnstile))

    def test_darknet_output_parsing_and_ambiguity_filtering(self) -> None:
        # Darknet raw console output simulation
        sample_output = """
        temp.jpg: Predicted in 68.12 milli-seconds.
        e: 98%
        I: 95%
        h: 87%
        C: 99%
        Y: 82%
        C: 94%
        1: 91%
        """
        parsed = DarknetYoloSolver.parse_darknet_output(sample_output)
        self.assertIsNotNone(parsed)
        # Ambiguous capital 'I' must be pruned (collides with lowercase 'l')
        self.assertNotIn("I", parsed["text"])
        # Exactly 6 characters
        self.assertEqual(len(parsed["text"]), 6)
        self.assertEqual(parsed["text"], "ehCYC1")
        self.assertGreater(parsed["confidence"], 0.8)

    def test_darknet_runs_only_in_shadow(self) -> None:
        # OCR proposes but never submits: without a person to answer, the
        # challenge stays unsolved, and the proposal is only recorded.
        from unittest.mock import patch
        manager = CaptchaManager()
        darknet = next(s for s in manager.solvers if s.solver_id == "darknet_yolo")
        darknet.mock_evaluator = lambda b: "7k9p2x"
        manager.interactive_ui.enabled = False

        challenge = CaptchaChallenge(
            provider_id="keep2share.cc",
            captcha_type=CaptchaType.IMAGE_TEXT,
            params={"image_data": b"GIF89a_fake_image_bytes"},
        )
        loop = asyncio.new_event_loop()
        try:
            with patch("engine.telemetry.telemetry_bus.record") as record:
                with self.assertRaises(RuntimeError):
                    loop.run_until_complete(manager.request_solution(challenge))
            shadow = [c for c in record.call_args_list if "[CAPTCHA_SHADOW]" in c.kwargs.get("message", "")]
            self.assertTrue(any(c.kwargs["context"]["solver"] == "darknet_yolo" for c in shadow))
            self.assertNotIn("7k9p2x", str(record.call_args_list), "the proposal itself is never logged")
            self.assertIsNone(challenge.solver_used)
            self.assertNotEqual(challenge.status, CaptchaStatus.SOLVED)
        finally:
            loop.close()

    # =========================================================================
    # 4. VPN / Route Hardening & WireGuard Tests
    # =========================================================================

    def test_route_health_socket_precheck_fast_fail(self) -> None:
        class DummyStore:
            def list_route_profiles(self):
                return [{
                    "id": "unreachable_vpn",
                    "kind": "socks5",
                    "endpoint": "socks5://127.0.0.1:59999",  # Port where nothing is listening
                    "enabled": True,
                }]

        manager = RouteManager(DummyStore())
        health = manager.health("unreachable_vpn")
        # Should fail fast without hanging
        self.assertFalse(health["healthy"])
        self.assertIn("Endpoint unreachable", health.get("error", ""))

    def test_parse_wireguard_configuration(self) -> None:
        wg_conf = """
        [Interface]
        PrivateKey = yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk=
        Address = 10.64.222.15/32,fd64:222::15/128
        DNS = 193.138.218.74

        [Peer]
        PublicKey = xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg=
        Endpoint = 194.38.20.2:51820
        AllowedIPs = 0.0.0.0/0, ::/0
        """
        parsed = RouteManager.parse_wireguard_conf(wg_conf)
        self.assertEqual(parsed["endpoint"], "194.38.20.2:51820")
        self.assertIn("10.64.222.15", parsed["address"])
        self.assertEqual(parsed["dns"], "193.138.218.74")


if __name__ == "__main__":
    unittest.main()
