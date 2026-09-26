"""Solvers are routed by declared contract, and every one gets a reason."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine import challenge_routing as routing  # noqa: E402
from engine.captcha import CaptchaChallenge, CaptchaManager, CaptchaSolver, CaptchaType  # noqa: E402
from engine.challenge_routing import Health, SolverCapability  # noqa: E402


class _Solver(CaptchaSolver):
    def __init__(self, solver_id, cap, *, enabled=True, healthy=True, accepts=True):
        super().__init__(solver_id, solver_id, enabled=enabled)
        self._cap, self._healthy, self._accepts = cap, healthy, accepts

    def capability(self):
        return self._cap

    def health(self):
        return Health(True) if self._healthy else Health(False, "service down")

    def can_solve(self, challenge):
        return self._accepts


TURNSTILE = SolverCapability(frozenset({"turnstile"}), "clearance", "reusable_clearance", inputs={"*": (routing.PAGE_INPUTS,)})


def _turnstile(params=None):
    return CaptchaChallenge(id="c1", captcha_type=CaptchaType.TURNSTILE, params=params if params is not None else {"page_url": "https://h.test/"})


class RouterTests(unittest.TestCase):
    def test_every_candidate_has_a_disposition_and_only_compatible_ones_run(self):
        solvers = [
            _Solver("ok", TURNSTILE),
            _Solver("off", TURNSTILE, enabled=False),
            _Solver("sick", TURNSTILE, healthy=False),
            _Solver("images", SolverCapability(frozenset({"image_text"}), "text", "answer_text")),
            _Solver("ocr", SolverCapability(frozenset({"turnstile"}), "text", "answer_text", production=False)),
            _Solver("person", SolverCapability(frozenset({"turnstile"}), "token", "single_use_token", manual=True)),
            _Solver("fussy", TURNSTILE, accepts=False),
            _Solver("undeclared", None),
        ]
        plan = routing.route(_turnstile(), solvers, automation_enabled=True, type_enabled=True)
        reasons = {d.solver_id: (d.disposition, d.reason, d.detail) for d in plan.decisions}
        self.assertEqual([s.solver_id for s in plan.automated], ["ok"])
        self.assertEqual(reasons["off"][1], routing.DISABLED)
        self.assertEqual(reasons["sick"][1:], (routing.UNHEALTHY, "service down"))
        self.assertEqual(reasons["images"][1], routing.UNSUPPORTED_TYPE)
        self.assertEqual(reasons["ocr"][:2], ("shadow", routing.SHADOW_ONLY))
        self.assertEqual(reasons["person"][:2], ("manual", routing.MANUAL))
        self.assertEqual(reasons["fussy"][1], routing.DECLINED)
        self.assertEqual(reasons["undeclared"][1], routing.NO_DECLARATION)
        self.assertEqual(len(plan.decisions), len(solvers), "no candidate disappears")

    def test_missing_inputs_and_user_switches_are_reasons_not_silence(self):
        plan = routing.route(_turnstile({}), [_Solver("ok", TURNSTILE)], automation_enabled=True, type_enabled=True)
        self.assertEqual((plan.decisions[0].reason, plan.decisions[0].detail), (routing.MISSING_INPUT, "page_url/url"))
        self.assertTrue(plan.manual_required)
        plan = routing.route(_turnstile(), [_Solver("ok", TURNSTILE)], automation_enabled=False, type_enabled=True)
        self.assertEqual(plan.decisions[0].reason, routing.AUTOMATION_OFF)
        plan = routing.route(_turnstile(), [_Solver("ok", TURNSTILE)], automation_enabled=True, type_enabled=False)
        self.assertEqual(plan.decisions[0].reason, routing.TYPE_OFF)

    def test_every_real_solver_declares_a_contract(self):
        manager = CaptchaManager()
        undeclared = [s.solver_id for s in manager.solvers if s.capability() is None]
        self.assertEqual(undeclared, [])
        ocr = {s.solver_id for s in manager.solvers if not s.capability().production}
        self.assertEqual(ocr, {"local_ocr", "darknet_yolo", "ddddocr"}, "OCR is shadow-only")

    def test_the_decisions_are_logged(self):
        with patch("engine.challenge_routing.telemetry_bus.record") as record:
            routing.route(_turnstile(), [_Solver("off", TURNSTILE, enabled=False)], automation_enabled=True, type_enabled=True)
        context = record.call_args.kwargs["context"]
        self.assertEqual(context["decisions"][0]["reason"], routing.DISABLED)


if __name__ == "__main__":
    unittest.main()
