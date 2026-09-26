"""Phase 25 gate: detection, routing, secrecy, OCR shadowing, lifecycle, fairness.

Runs the dated response corpus through the passive classifier and fails on a
measurable regression: per-family recall below 100%, any false positive,
unstable evidence codes, altered bytes, leaked response material, a silent
routing skip, an OCR answer that could be submitted, a lifecycle that cannot
recover, or solver work that exceeds its bound.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import challenge_lifecycle as lifecycle  # noqa: E402
from engine import challenge_routing as routing  # noqa: E402
from engine.captcha import CaptchaChallenge, CaptchaManager, CaptchaType  # noqa: E402
from engine.challenge_classifier import ReplayInspectableStream, classify, is_inspectable_text, response_observation  # noqa: E402
from engine.challenge_scheduler import ChallengeScheduler  # noqa: E402

CORPUS = json.loads((ROOT / "tests" / "fixtures" / "challenges" / "responses-v1.json").read_text(encoding="utf-8"))


def _verdict(case):
    body = case["body"].encode("utf-8")
    observation = response_observation(status=case["status"], headers=case["headers"], final_url="https://site.test/x",
                                       body_prefix=body if is_inspectable_text(case["headers"]) else b"")
    return classify(observation)


class DetectionMatrix(unittest.TestCase):
    def test_recall_and_false_positives_by_family(self):
        report: dict[str, dict[str, int]] = {}
        false_positives = []
        for case in CORPUS["cases"]:
            verdict = _verdict(case)
            family = case["family"]
            if family is None:
                if verdict.outcome == "challenge":
                    false_positives.append(case["id"])
                continue
            row = report.setdefault(family, {"cases": 0, "detected": 0})
            row["cases"] += 1
            row["detected"] += int(verdict.outcome == "challenge" and verdict.family == family)
        recall = {f: r["detected"] / r["cases"] for f, r in report.items()}
        self.assertEqual(recall, {f: 1.0 for f in report}, f"recall by family: {recall}")
        self.assertEqual(false_positives, [], "non-challenge responses classified as challenges")

    def test_evidence_is_stable_and_carries_no_response_material(self):
        for case in CORPUS["cases"]:
            first, second = _verdict(case), _verdict(case)
            self.assertEqual(first, second, case["id"])
            for evidence in first.evidence:
                self.assertNotIn(evidence.code.lower(), case["body"].lower().replace("rule:", "~"), case["id"])
                self.assertRegex(evidence.code, r"^[a-z0-9:_\-]+$")

    def test_low_confidence_never_starts_automation(self):
        for case in CORPUS["cases"]:
            verdict = _verdict(case)
            if verdict.confidence != "high":
                self.assertFalse(verdict.automation_eligible, case["id"])

    def test_inspected_bytes_reach_the_consumer_exactly_once(self):
        for case in CORPUS["cases"]:
            body = case["body"].encode("utf-8") * 200  # past the 64 KiB inspection bound
            observation = response_observation(status=case["status"], headers=case["headers"], final_url="https://site.test/x")
            chunks = (body[i:i + 7000] for i in range(0, len(body), 7000))
            stream = ReplayInspectableStream(chunks, observation, is_inspectable_text(case["headers"]))
            self.assertEqual(b"".join(stream.iter_bytes()), body, case["id"])


class RoutingAndSecrecyMatrix(unittest.TestCase):
    def test_every_solver_is_accounted_for_on_every_family(self):
        manager = CaptchaManager()
        for kind in CaptchaType:
            challenge = CaptchaChallenge(captcha_type=kind, params={"page_url": "https://site.test/", "site_key": "k", "image_data": "x"})
            plan = routing.route(challenge, manager.solvers, automation_enabled=True, type_enabled=True)
            self.assertEqual({d.solver_id for d in plan.decisions}, {s.solver_id for s in manager.solvers}, kind)
            self.assertTrue(all(d.reason for d in plan.decisions))
            self.assertFalse({s.solver_id for s in plan.automated} & {"local_ocr", "darknet_yolo", "ddddocr"},
                             "OCR never reaches production")

    def test_lifecycle_recovers_and_refuses_stale_commands(self):
        challenge = CaptchaChallenge(id="m1", captcha_type=CaptchaType.TURNSTILE)
        for state in (lifecycle.ROUTING, lifecycle.SOLVING, lifecycle.VERIFYING):
            self.assertTrue(lifecycle.advance(challenge, state, "test"))
        self.assertFalse(lifecycle.advance(challenge, lifecycle.SOLVING, "illegal"), "verifying cannot go back to solving")
        self.assertEqual(lifecycle.command_outcome(challenge, 1), "duplicate")
        self.assertTrue(lifecycle.recover_after_restart(challenge))
        self.assertEqual(lifecycle.state_of(challenge), lifecycle.MANUAL)
        self.assertEqual(lifecycle.command_outcome(challenge, 1), "stale", "a click on the old attempt is refused")
        self.assertIsNone(lifecycle.command_outcome(challenge, 2))

    def test_solver_work_is_bounded_and_first_come_first_served(self):
        scheduler = ChallengeScheduler(limit=2)
        peak, order = [0], []

        async def work(name):
            peak[0] = max(peak[0], scheduler.running)
            order.append(name)
            await asyncio.sleep(0.01)

        async def main():
            await asyncio.gather(*(scheduler.run(CaptchaChallenge(id=f"f{i}"), lambda i=i: work(i)) for i in range(6)))

        asyncio.run(main())
        self.assertEqual(peak[0], 2)
        self.assertEqual(order, list(range(6)))
        self.assertEqual((scheduler.running, scheduler.waiting), (0, 0))


if __name__ == "__main__":
    unittest.main()
