"""Browser handoff security (VER-02): only a fresh, matching, single-use return counts."""
from __future__ import annotations

import sys
import time
import unittest
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine import challenge_lifecycle as lifecycle  # noqa: E402
from engine.captcha import CaptchaChallenge, CaptchaManager, CaptchaStatus, CaptchaType  # noqa: E402
from engine.challenge_handoff import HANDOFF_VERSION, HandoffBroker  # noqa: E402

TOKEN = "0.WIDGET-ANSWER-" + "x" * 40


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.manager = CaptchaManager()
        self.challenge = CaptchaChallenge(id="h1", task_id="t1", captcha_type=CaptchaType.TURNSTILE,
                                          params={"page_url": "https://files.test/d/abc?x=1"})
        self.manager.register_challenge(self.challenge)
        lifecycle.advance(self.challenge, lifecycle.MANUAL, "needs the user")
        self.broker = HandoffBroker(self.manager)
        opened = self.broker.open("h1", 1)
        self.assertEqual(opened["outcome"], "opened")
        fragment = urllib.parse.parse_qs(urllib.parse.urlsplit(opened["url"]).fragment)
        self.ticket = fragment["mossdl-handoff"][0]
        self.assertTrue(opened["url"].startswith("https://files.test/d/abc?x=1#"), "only the challenge's own page is opened")

    def ret(self, **changes):
        message = {"version": HANDOFF_VERSION, "ticket": self.ticket, "challenge_id": "h1", "generation": 1,
                   "origin": "https://files.test", "profile": "default", "token": TOKEN,
                   "cookies": [{"name": "cf_clearance", "value": "cl", "domain": ".files.test"}]}
        message.update(changes)
        return self.broker.redeem(message)["outcome"]

    def test_a_matching_return_is_accepted_once_and_only_moves_to_verifying(self):
        self.assertEqual(self.ret(), "accepted")
        self.assertEqual(self.challenge.status, CaptchaStatus.VERIFYING, "the site still has to accept it")
        self.assertEqual(self.ret(), "replayed")

    def test_forged_stale_and_mismatched_returns_are_refused(self):
        self.assertEqual(self.ret(ticket="forged-" + "a" * 40), "unknown_ticket")
        self.assertEqual(self.ret(generation=2), "mismatched")
        self.assertEqual(self.ret(origin="https://evil.test"), "mismatched")
        self.assertEqual(self.ret(profile="other"), "mismatched")
        self.assertEqual(self.ret(challenge_id="h2"), "mismatched")
        self.assertEqual(self.ret(cookies=[{"name": "sid", "value": "v", "domain": ".evil.test"}]), "mismatched")
        self.assertEqual(self.ret(version="mossdl-handoff/0"), "invalid")
        self.assertEqual(self.ret(token="x" * 70_000), "invalid")
        self.assertEqual(self.ret(cookies=[{"name": "n", "value": "v"}] * 51), "invalid")
        self.assertEqual(self.ret(token=None, cookies=[]), "invalid")
        self.assertEqual(self.ret(), "accepted", "failed attempts did not burn the valid ticket")

    def test_cancelled_and_expired_tickets_are_refused(self):
        self.broker.cancel("h1")
        self.assertEqual(self.ret(), "cancelled")
        opened = self.broker.open("h1", 1)
        self.ticket = urllib.parse.parse_qs(urllib.parse.urlsplit(opened["url"]).fragment)["mossdl-handoff"][0]
        with patch("engine.challenge_handoff.time.time", return_value=time.time() + 3600):
            self.assertEqual(self.ret(), "expired")

    def test_a_new_attempt_makes_old_tickets_stale(self):
        lifecycle.advance(self.challenge, lifecycle.VERIFYING, "answered elsewhere")
        lifecycle.advance(self.challenge, lifecycle.REJECTED, "site said no")
        lifecycle.advance(self.challenge, lifecycle.MANUAL, "answer again")
        self.assertEqual(self.ret(), "mismatched")
        self.assertEqual(self.broker.open("h1", 1)["outcome"], "denied", "opening for an old generation is refused")

    def test_nothing_but_the_challenge_page_can_be_opened(self):
        for page in ("file:///C:/Windows/System32/calc.exe", "javascript:alert(1)", ""):
            challenge = CaptchaChallenge(id=f"p{len(page)}", captcha_type=CaptchaType.TURNSTILE, params={"page_url": page})
            self.manager.register_challenge(challenge)
            self.assertEqual(self.broker.open(challenge.id)["outcome"], "unavailable", page)

    def test_tickets_and_answers_never_reach_logs(self):
        with patch("engine.challenge_handoff.telemetry_bus.record") as record:
            self.ret(origin="https://evil.test")
            self.ret()
        logged = str(record.call_args_list)
        self.assertNotIn(self.ticket, logged)
        self.assertNotIn(TOKEN, logged)
        self.assertNotIn(self.ticket, str(self.challenge.to_dict()))


class ServiceHandoffTests(unittest.TestCase):
    def test_an_accepted_return_resumes_only_its_task_to_be_verified(self):
        from engine.models import DownloadTask
        from engine.service import EngineService
        with TemporaryDirectory() as data_dir:
            service = EngineService(data_dir)
            try:
                task = DownloadTask(id="task-h", source_url="https://files.test/d/abc", destination="downloads")
                task.state, task.user_action = "needs_user", "turnstile"
                service.store.save(task)
                challenge = CaptchaChallenge(id="svc-h", task_id="task-h", captcha_type=CaptchaType.TURNSTILE,
                                             params={"page_url": "https://files.test/d/abc"})
                service.captcha.register_challenge(challenge)
                lifecycle.advance(challenge, lifecycle.MANUAL, "needs the user")
                opened = service.dispatch("captcha_handoff_open", {"challenge_id": "svc-h", "generation": 1})
                ticket = urllib.parse.parse_qs(urllib.parse.urlsplit(opened["url"]).fragment)["mossdl-handoff"][0]
                result = service.dispatch("captcha_handoff_return", {"handoff": {
                    "version": HANDOFF_VERSION, "ticket": ticket, "challenge_id": "svc-h", "generation": 1,
                    "origin": "https://files.test", "profile": "default", "token": TOKEN, "cookies": []}})
                self.assertEqual(result["outcome"], "accepted")
                resumed = service.store.get("task-h")
                self.assertIn(resumed.state, {"queued", "resolving"})
                self.assertTrue(resumed.user_challenge.get("verifying"), "resolved only once the site accepts")
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
