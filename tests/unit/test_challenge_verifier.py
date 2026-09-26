"""An answer is solved only when the site accepts it; artifacts keep their owners."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine import challenge_artifacts as artifacts  # noqa: E402
from engine import challenge_lifecycle as lifecycle  # noqa: E402
from engine.captcha import CaptchaChallenge, CaptchaManager, CaptchaStatus, CaptchaType  # noqa: E402

SOLUTION = {"token": "SECRET-TOKEN-123", "cf_clearance": "CLEARANCE", "user_agent": "UA/1",
            "cookies": {"cf_clearance": "CLEARANCE", "__cf_bm": "BM", "file_code": "abc", "PHPSESSID": "sess"},
            "direct_url": "https://cdn.test/f.zip?sig=1"}


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.manager = CaptchaManager()
        self.challenge = CaptchaChallenge(id="v1", task_id="t1", captcha_type=CaptchaType.TURNSTILE,
                                          params={"page_url": "https://h.test/"})
        self.manager.register_challenge(self.challenge)
        self.accepted, self.rejected = [], []
        self.manager.verifier.on_accept = self.accepted.append
        self.manager.verifier.on_reject = self.rejected.append

    def test_received_is_not_solved_until_the_site_accepts(self):
        self.assertTrue(self.manager.solve_challenge("v1", dict(SOLUTION), solver_id="automated_browser"))
        self.assertEqual(self.challenge.status, CaptchaStatus.VERIFYING)
        self.assertFalse(self.manager.solve_challenge("v1", dict(SOLUTION)), "a second answer is refused while one is checked")
        with patch("engine.challenge_verifier.telemetry_bus.record") as record:
            self.assertTrue(self.manager.verifier.accept("v1", "the site served the download"))
        self.assertEqual(self.challenge.status, CaptchaStatus.SOLVED)
        self.assertEqual(lifecycle.state_of(self.challenge), lifecycle.RESOLVED)
        self.assertEqual(self.accepted, [self.challenge])
        self.assertNotIn("SECRET-TOKEN-123", str(record.call_args_list), "verdicts never log the answer")
        self.assertFalse(self.manager.verifier.accept("v1", "again"), "a settled challenge takes no second verdict")

    def test_a_rejected_answer_is_dropped_and_reported(self):
        self.manager.solve_challenge("v1", dict(SOLUTION), solver_id="automated_browser")
        with patch.object(self.manager, "report_result") as report:
            self.manager.verifier.reject("v1", "the site asked again")
        report.assert_called_once_with("v1", False)
        self.assertEqual(self.challenge.status, CaptchaStatus.FAILED)
        self.assertIsNone(self.challenge.solution)
        self.assertEqual(self.rejected, [self.challenge])
        # The next attempt is a new generation.
        self.assertTrue(lifecycle.advance(self.challenge, lifecycle.MANUAL, "answer again"))
        self.assertEqual(lifecycle.generation_of(self.challenge), 2)

    def test_the_public_shape_never_carries_secrets(self):
        self.manager.solve_challenge("v1", dict(SOLUTION), solver_id="automated_browser")
        self.assertNotIn("SECRET-TOKEN-123", str(self.challenge.to_dict()))
        self.assertEqual(self.challenge.to_dict()["lifecycle"]["state"], "verifying")


class ArtifactTests(unittest.TestCase):
    def test_only_clearance_may_cross_tasks(self):
        shared = artifacts.shareable_session(SOLUTION)
        self.assertEqual(shared["cookies"], {"cf_clearance": "CLEARANCE", "__cf_bm": "BM"})
        self.assertNotIn("SECRET-TOKEN-123", str(shared))
        self.assertNotIn("cdn.test", str(shared))
        self.assertNotIn("PHPSESSID", str(shared), "per-file session cookies stay with their task")

    def test_solutions_split_into_owned_kinds(self):
        parts = artifacts.split_solution(SOLUTION)
        self.assertEqual(parts[artifacts.SINGLE_USE_TOKEN], "SECRET-TOKEN-123")
        self.assertIn("file_code", parts[artifacts.FORM_SESSION])
        self.assertEqual(parts[artifacts.DIRECT_URL], SOLUTION["direct_url"])
        for kind in (artifacts.SINGLE_USE_TOKEN, artifacts.FORM_SESSION, artifacts.DIRECT_URL, artifacts.CONTINUATION):
            policy = artifacts.POLICIES[kind]
            self.assertFalse(policy.crosses_tasks or policy.durable or policy.public, kind)
        self.assertNotIn("SECRET", str(artifacts.redacted_summary(SOLUTION)))

    def test_clearance_reuse_needs_a_coherent_identity(self):
        stored = {"route": "direct", "user_agent": "UA/1", "impersonate": "chrome"}
        self.assertEqual(artifacts.reuse_decision(stored, {**stored}), (True, "identity_match"))
        self.assertEqual(artifacts.reuse_decision(stored, {**stored, "route": "socks5h://vpn"})[1], "route_mismatch")
        self.assertEqual(artifacts.reuse_decision(stored, {**stored, "user_agent": "UA/2"})[1], "user_agent_mismatch")
        self.assertEqual(artifacts.reuse_decision(stored, {**stored, "impersonate": "firefox"})[1], "tls_profile_mismatch")

    def test_the_http_cache_refuses_clearance_from_another_route(self):
        from engine import route_http
        from engine.http_client import ClearanceCache
        cache = ClearanceCache()
        self.addCleanup(route_http.set_active_route, "direct", None)
        route_http.set_active_route("direct", None)
        cache.set_clearance("https://h.test/", {"cf_clearance": "x"}, "UA/1")
        self.assertIsNotNone(cache.get_clearance("https://h.test/"))
        route_http.set_active_route("vpn", "socks5h://127.0.0.1:1")
        self.assertIsNone(cache.get_clearance("https://h.test/"), "clearance is bound to the address that earned it")
        route_http.set_active_route("direct", None)
        self.assertIsNone(cache.get_clearance("https://h.test/", user_agent="UA/2"))
        cache.quarantine("h.test")
        self.assertIsNone(cache.get_clearance("https://h.test/"))


class OcrEvaluationTests(unittest.TestCase):
    def test_ocr_is_measured_and_the_gate_stays_closed(self):
        from engine.captcha import LocalOcrSolver
        from engine.challenge_ocr_evaluation import evaluate, load_corpus
        root = Path(__file__).resolve().parents[1]
        report = evaluate(load_corpus(root / "fixtures" / "challenges" / "ocr-corpus-v1.json"), [LocalOcrSolver()])
        self.assertEqual(report["collected_on"], "2026-09-25")
        math = next(s for s in report["scores"] if s["family"] == "text_math")
        self.assertEqual(math["exact_accuracy"], 1.0)
        self.assertIn("false_acceptance_rate", math)
        self.assertFalse(report["approval_gate"]["production_eligible"])

    def test_an_incomplete_corpus_is_refused(self):
        import json
        import tempfile
        from engine.challenge_ocr_evaluation import load_corpus
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"version": "x", "cases": []}, handle)
        with self.assertRaises(ValueError):
            load_corpus(handle.name)


if __name__ == "__main__":
    unittest.main()
