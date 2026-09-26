"""The resolve-phase benchmark: parallelism and stage churn.

Getting every part from queued to `direct_link_acquired` is frequently the real
cost of a multipart package, not the transport. Nothing measured it, so neither
the overlap between parts nor the stage churn within a part was visible.
"""

from __future__ import annotations

import unittest

from tests.live.resolve_phase import EXPECTED_CHURN, build_resolve_phase

T0 = 1_000_000.0


def _stage(task: str, stage: str, at: float) -> dict:
    return {"message": f"[{task}] (lifecycle) [{stage.upper()}]",
            "context": {"stage": "lifecycle", "task_id": task, "to_stage": stage,
                        "entered_at_us": T0 + at}}


def _clean_part(task: str, start: float, solve_at: float, link_at: float) -> list[dict]:
    return [
        _stage(task, "resolving_metadata", start),
        _stage(task, "captcha_challenge_detected", start + 2),
        _stage(task, "captcha_solving", solve_at),
        _stage(task, "direct_link_acquired", link_at),
    ]


class ResolvePhaseTests(unittest.TestCase):
    def test_no_resolved_parts_is_reported(self) -> None:
        block = build_resolve_phase([_stage("a", "resolving_metadata", 0)])
        self.assertEqual(block["per_task"], [])
        self.assertTrue(any(n["code"] == "no_resolved_parts" for n in block["notes"]))

    def test_single_part_duration(self) -> None:
        block = build_resolve_phase(_clean_part("a", 0, 5, 50))
        self.assertEqual(block["summary"]["parts"], 1)
        self.assertAlmostEqual(block["summary"]["mean_seconds"], 50.0, places=2)
        self.assertEqual(block["summary"]["overlap_factor"], 1.0)

    def test_serial_parts_have_overlap_factor_one(self) -> None:
        """Strictly serial resolution: naive sum equals wall span."""
        events = _clean_part("a", 0, 5, 50) + _clean_part("b", 50, 55, 100)
        summary = build_resolve_phase(events)["summary"]
        self.assertAlmostEqual(summary["wall_seconds"], 100.0, places=2)
        self.assertAlmostEqual(summary["naive_sum_seconds"], 100.0, places=2)
        self.assertAlmostEqual(summary["overlap_factor"], 1.0, places=2)

    def test_fully_parallel_parts_report_the_part_count(self) -> None:
        """Three parts resolving together: overlap == 3, efficiency == 1."""
        events = _clean_part("a", 0, 5, 50) + _clean_part("b", 0, 5, 50) + _clean_part("c", 0, 5, 50)
        summary = build_resolve_phase(events)["summary"]
        self.assertAlmostEqual(summary["overlap_factor"], 3.0, places=2)
        self.assertAlmostEqual(summary["parallel_efficiency"], 1.0, places=2)
        self.assertAlmostEqual(summary["ideal_wall_seconds"], 50.0, places=2)

    def test_partial_overlap_is_between_one_and_n(self) -> None:
        events = _clean_part("a", 0, 5, 60) + _clean_part("b", 30, 35, 90)
        summary = build_resolve_phase(events)["summary"]
        self.assertGreater(summary["overlap_factor"], 1.0)
        self.assertLess(summary["overlap_factor"], 2.0)

    def test_churn_counts_resolve_stage_transitions(self) -> None:
        """A part that ping-pongs between stages is flagged as excess churn."""
        noisy = [
            _stage("a", "resolving_metadata", 0),
            _stage("a", "captcha_challenge_detected", 2),
            _stage("a", "captcha_solving", 3),
            _stage("a", "resolving_metadata", 5),
            _stage("a", "resolving_metadata", 7),
            _stage("a", "captcha_challenge_detected", 20),
            _stage("a", "captcha_verifying", 24),
            _stage("a", "hoster_wait_timer", 25),
            _stage("a", "captcha_solving", 40),
            _stage("a", "direct_link_acquired", 52),
        ]
        summary = build_resolve_phase(noisy)["summary"]
        self.assertEqual(summary["stage_transitions_max"], 9)
        self.assertEqual(summary["excess_transitions"], 9 - EXPECTED_CHURN)

    def test_clean_part_has_no_excess_churn(self) -> None:
        summary = build_resolve_phase(_clean_part("a", 0, 5, 50))["summary"]
        self.assertEqual(summary["excess_transitions"], 0,
                         "a four-transition resolve was reported as churning")

    def test_solver_launches_and_challenges_are_counted(self) -> None:
        events = _clean_part("a", 0, 5, 50) + [
            {"message": "[SOLVER_LAUNCHED] Clearcote Chromium active", "context": {}},
            {"message": "[CAPTCHA_CHALLENGE] part1 requires TURNSTILE", "context": {}},
            {"message": "[CAPTCHA_CHALLENGE] part2 requires TURNSTILE", "context": {}},
        ]
        summary = build_resolve_phase(events)["summary"]
        self.assertEqual(summary["solver_launches"], 1)
        # The clean part's own stage record also carries the marker text.
        self.assertGreaterEqual(summary["challenges"], 2)

    def test_records_without_timestamps_are_skipped_not_crashed(self) -> None:
        events = _clean_part("a", 0, 5, 50) + [
            {"context": {"stage": "lifecycle", "task_id": "b", "to_stage": "direct_link_acquired"}},
        ]
        summary = build_resolve_phase(events)["summary"]
        self.assertEqual(summary["parts"], 1)


if __name__ == "__main__":
    unittest.main()
