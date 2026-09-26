import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace

from engine import activity_history as ah
from engine.captcha import CaptchaChallenge, CaptchaStatus, CaptchaType
from engine.service import EngineService


class TestCaptchaHistoryShape(unittest.TestCase):
    def test_outcomes_collapse_engine_statuses(self):
        self.assertEqual(ah.captcha_outcome("solved", None), "solved")
        self.assertEqual(ah.captcha_outcome("failed", None), "failed")
        self.assertEqual(ah.captcha_outcome("expired", None), "stalled")
        self.assertEqual(ah.captcha_outcome("skipped", None), "skipped")
        self.assertEqual(ah.captcha_outcome("pending", 200.0, now=100.0), "pending")
        # A pending row past its deadline was abandoned.
        self.assertEqual(ah.captcha_outcome("pending", 50.0, now=100.0), "stalled")

    def test_vendor_and_labels(self):
        self.assertEqual(ah.captcha_vendor("turnstile"), "cloudflare")
        self.assertEqual(ah.captcha_vendor("CaptchaType.TURNSTILE"), "cloudflare")
        self.assertEqual(ah.captcha_vendor("recaptcha_v2"), "google")
        self.assertEqual(ah.captcha_vendor("hcaptcha_audio"), "hcaptcha")
        self.assertEqual(ah.captcha_vendor("image_text"), "image")
        self.assertEqual(ah.solver_label("clearcote"), "Clearcote")
        self.assertEqual(ah.solver_label("interactive_ui"), "You")

    def test_entry_never_exposes_cookies_or_signed_params(self):
        entry = ah.captcha_history_entry({
            "id": "c1", "task_id": "t1", "captcha_type": "turnstile", "status": "solved",
            "solver_id": "clearcote", "created_at": 10.0, "resolved_at": 16.5, "expires_at": 100.0,
            "params": {"page_url": "https://host.example/file?id=1&token=abc", "cookies": {"cf": "secret"},
                       "host": "host.example"},
        })
        self.assertEqual(entry["duration_seconds"], 6.5)
        self.assertEqual(entry["page_url"], "https://host.example/file?id=1")
        self.assertNotIn("params", entry)
        self.assertNotIn("secret", repr(entry))


class TestShortlinkHistory(unittest.TestCase):
    def test_groups_hops_by_task_newest_first(self):
        rows = [
            {"task_id": "a", "hop": 1, "host": "b.example", "state": "visited", "updated_at": 5.0},
            {"task_id": "a", "hop": 0, "host": "a.example", "state": "visited", "updated_at": 4.0},
            {"task_id": "z", "hop": 0, "host": "x.example", "state": "failed", "error": "boom", "updated_at": 9.0},
        ]
        chains = ah.shortlink_history(rows)
        self.assertEqual([chain["task_id"] for chain in chains], ["z", "a"])
        self.assertEqual([hop["host"] for hop in chains[1]["hops"]], ["a.example", "b.example"])
        self.assertTrue(chains[0]["failed"])
        self.assertFalse(chains[1]["failed"])


class TestTaskTimeline(unittest.TestCase):
    def test_merges_sources_flags_stalls_and_drops_noise(self):
        task = SimpleNamespace(
            id="t1", state="failed", error="host offline", finished_at=300.0,
            stage_history=[
                {"to": "resolving_metadata", "entered_at": 100.0},
                {"to": "captcha_challenge_detected", "entered_at": 110.0},
                {"to": "failed", "entered_at": 290.0},
            ],
        )
        timeline = ah.build_task_timeline(
            task,
            transitions=[
                {"to_state": "failed", "reason": "host offline", "created_at": 290.0},
                {"to_state": "failed", "reason": "host offline", "created_at": 290.1},
                {"to_state": "resolving", "reason": None, "created_at": 100.0},
            ],
            challenges=[{"id": "c1", "task_id": "t1", "captcha_type": "hcaptcha", "status": "expired",
                         "created_at": 110.0, "expires_at": 200.0, "params": {}}],
            chain=[{"hop": 0, "host": "short.example", "state": "visited", "updated_at": 105.0}],
            attempts=[{"provider_id": "hostx", "outcome": "failed", "started_at": 250.0,
                       "ended_at": 260.0, "error": "503"}],
            diagnostics=[
                {"level": "warn", "stage": "lifecycle", "message": "internal", "created_at": 111.0},
                {"level": "error", "stage": "download", "message": "socket reset", "created_at": 280.0},
            ],
            now=400.0,
        )
        kinds = [event["kind"] for event in timeline["events"]]
        self.assertEqual(kinds.count("state"), 1)  # duplicate failure collapsed
        self.assertNotIn("internal", [event["title"] for event in timeline["events"]])
        self.assertIn("socket reset", [event["title"] for event in timeline["events"]])
        self.assertEqual(timeline["stalled_at"], "CAPTCHA detected")
        self.assertEqual(timeline["captchas"], {"total": 1, "solved": 0, "failed": 1})
        self.assertEqual(timeline["shortlink_hops"], 1)
        self.assertEqual(timeline["duration_seconds"], 200.0)
        self.assertEqual([event["at"] for event in timeline["events"]],
                         sorted(event["at"] for event in timeline["events"]))


class TestServiceActivityRpcs(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.service = EngineService(self.temp_dir)

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_captcha_history_lists_resolved_challenges(self):
        challenge = CaptchaChallenge(id="hist-1", captcha_type=CaptchaType.TURNSTILE,
                                     params={"page_url": "https://host.example/f"},
                                     status=CaptchaStatus.SOLVED, solver_used="clearcote",
                                     resolved_at=None)
        self.service.store.save_captcha_challenge(challenge)
        history = self.service.dispatch("captcha_history", {"limit": 10})
        self.assertEqual(history[0]["id"], "hist-1")
        self.assertEqual(history[0]["vendor"], "cloudflare")
        self.assertEqual(history[0]["solver_label"], "Clearcote")
        self.assertEqual(self.service.dispatch("shortlink_history"), [])
        self.assertEqual(self.service.dispatch("task_activity_counts", {"ids": []}), {})

    def test_banner_and_auto_solve_settings_are_accepted(self):
        result = self.service.dispatch("ui_settings_update", {"settings": {
            "captcha": {"captchaAutoSolve": True, "captchaShowAutoBanner": False}}})
        self.assertTrue(result["settings"]["captcha"]["captchaAutoSolve"])
        self.assertFalse(result["settings"]["captcha"]["captchaShowAutoBanner"])
        self.assertTrue(self.service._captcha_auto_solve_enabled())

    def test_manual_required_auto_solves_once_only_when_enabled(self):
        calls: list[tuple[str, dict]] = []
        done = threading.Event()
        original = self.service.dispatch

        def fake_dispatch(method, params=None):
            if method == "captcha_solve":
                calls.append((method, params))
                done.set()
                return {"success": True}
            return original(method, params)

        self.service.dispatch = fake_dispatch
        challenge = CaptchaChallenge(id="auto-1")

        self.service._on_captcha_manual_required(challenge)
        self.assertFalse(done.wait(0.3))
        self.assertEqual(calls, [])

        original("ui_settings_update", {"settings": {"captcha": {"captchaAutoSolve": True}}})
        self.service._on_captcha_manual_required(challenge)
        self.assertTrue(done.wait(2))
        self.service._on_captcha_manual_required(challenge)
        self.assertEqual(calls, [("captcha_solve", {"challenge_id": "auto-1"})])


if __name__ == "__main__":
    unittest.main()
