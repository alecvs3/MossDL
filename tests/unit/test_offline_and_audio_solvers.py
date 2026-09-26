from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import json
import unittest
from pathlib import Path

from engine.captcha import CaptchaChallenge, CaptchaManager, CaptchaType
from engine.ddddocr_solver import DdddOcrSolver
from engine.audio_solver import AudioChallengeSolver
from engine.flaresolverr_client import FlareResolution, FlareSolverrClient
from engine.service import EngineService
from engine.db import TaskStore


class OfflineAndAudioSolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path("test_scratch_solvers")
        self.temp_dir.mkdir(exist_ok=True)
        self.db_path = self.temp_dir / "test_store.db"
        self.store = TaskStore(str(self.db_path))

    def tearDown(self) -> None:
        import shutil
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ddddocr_model_discovery_and_fallback(self) -> None:
        solver = DdddOcrSolver()
        # A local ddddocr checkout's common.onnx is discovered (not redistributed; absent in CI).
        if (_ROOT / "clone_reference" / "ddddocr").is_dir():
            self.assertTrue(solver.is_model_available() or solver.model_path is not None)
        
        # Test math parsing without needing onnxruntime
        math_ch = CaptchaChallenge(
            captcha_type=CaptchaType.IMAGE_TEXT,
            params={"text_prompt": "What is 14 + 19?"}
        )
        self.assertTrue(solver.can_solve(math_ch))
        sol = asyncio.run(solver.solve(math_ch))
        self.assertIsNotNone(sol)
        self.assertEqual(sol["text"], "33")
        self.assertEqual(sol["method"], "ddddocr_math_eval")

    def test_ddddocr_mock_and_params(self) -> None:
        solver = DdddOcrSolver()
        ch = CaptchaChallenge(
            captcha_type=CaptchaType.IMAGE_TEXT,
            params={"mock_solution": "K9X7", "image_data": "data:image/png;base64,iVBORw0KGgo="}
        )
        sol = asyncio.run(solver.solve(ch))
        self.assertIsNotNone(sol)
        self.assertEqual(sol["text"], "K9X7")

    def test_audio_solver_normalization_and_formatting(self) -> None:
        solver = AudioChallengeSolver()
        raw = "Three One Seven, NINE!! Zero"
        normalized = solver._normalize_transcription(raw)
        self.assertEqual(normalized, "3 1 7 9 0")

    def test_audio_solver_mock_resolution(self) -> None:
        solver = AudioChallengeSolver()
        ch = CaptchaChallenge(
            captcha_type=CaptchaType.RECAPTCHA_AUDIO,
            params={"mock_solution": "four eight two five", "audio_bytes": b"RIFF....WAVE"}
        )
        self.assertTrue(solver.can_solve(ch))
        sol = asyncio.run(solver.solve(ch))
        self.assertIsNotNone(sol)
        self.assertEqual(sol["text"], "4 8 2 5")
        self.assertEqual(sol["method"], "mock_audio")

    def test_audio_solver_custom_audio_fetcher(self) -> None:
        fetched_urls = []
        def mock_fetch(url: str) -> bytes:
            fetched_urls.append(url)
            return b"MOCK_AUDIO_DATA"

        solver = AudioChallengeSolver(
            audio_fetcher=mock_fetch,
            wit_api_key="TEST_KEY"
        )
        ch = CaptchaChallenge(
            captcha_type=CaptchaType.RECAPTCHA_AUDIO,
            params={"audio_url": "https://google.com/recaptcha/api2/payload/audio.mp3", "mock_solution": "9 2 4"}
        )
        sol = asyncio.run(solver.solve(ch))
        self.assertIsNotNone(sol)
        self.assertEqual(sol["text"], "9 2 4")

    def test_flaresolverr_client_offline_handling(self) -> None:
        # Point to an unallocated port to verify fast failure (<1.0s)
        client = FlareSolverrClient(endpoint="http://127.0.0.1:59999", timeout=1.0)
        self.assertFalse(client.is_available())
        
        # Test resolve fast-fails gracefully
        res = asyncio.run(client.resolve("https://example.com/protected"))
        self.assertEqual(res.status, "error")
        self.assertIn("unreachable", res.message.lower())

    def test_flaresolverr_resolution_cookie_formatting(self) -> None:
        res = FlareResolution(
            url="https://target.com/final",
            status="ok",
            message="Challenge solved!",
            solution_cookies=[
                {"name": "cf_clearance", "value": "xyz12345"},
                {"name": "__cf_bm", "value": "bm9999"},
            ],
            user_agent="Mozilla/5.0 TestBrowser",
            response_html="<html><body>Ready</body></html>",
        )
        self.assertEqual(res.cf_clearance, "xyz12345")
        self.assertIn("cf_clearance=xyz12345", res.cookie_header())
        self.assertIn("__cf_bm=bm9999", res.cookie_header())

    def test_manager_cascade_contains_new_solvers(self) -> None:
        mgr = CaptchaManager(store=self.store)
        solver_ids = [s.solver_id for s in mgr.solvers]
        self.assertIn("ddddocr", solver_ids)
        self.assertIn("audio_speech", solver_ids)
        self.assertIn("darknet_yolo", solver_ids)
        self.assertIn("local_ocr", solver_ids)

    def test_service_flaresolverr_and_audio_rpc(self) -> None:
        service = EngineService(self.temp_dir / "service_data")
        try:
            # Check flaresolverr health RPC
            health = service.dispatch("flaresolverr_check_health")
            self.assertIn("available", health)
            self.assertIn("endpoint", health)

            # Configure audio solver RPC
            res = service.dispatch("audio_solver_configure", {
                "speech_service": "wit",
                "wit_api_key": "CUSTOM_KEY_123",
                "enabled": True,
            })
            self.assertTrue(res["configured"])
            self.assertEqual(res["speech_service"], "wit")
            self.assertTrue(res["enabled"])
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
