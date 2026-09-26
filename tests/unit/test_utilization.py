"""The utilization trace must attribute a bottleneck, not just report totals."""

from __future__ import annotations

import unittest

from tests.live.utilization import build_utilization

T0 = 1_000_000.0


def _solver(start: float, end: float) -> dict:
    return {"_challenge_epoch": T0 + start, "_solved_epoch": T0 + end}


def _timer(start: float, waited: float) -> dict:
    return {"_detected_epoch": T0 + start, "waited_seconds": waited}


def _ramp(at: float, active: int) -> dict:
    return {"_epoch": T0 + at, "active_streams": active}


class UtilizationSeriesTests(unittest.TestCase):
    def test_empty_inputs_are_reported_not_crashed(self) -> None:
        result = build_utilization(solvers=[], timers=[], ramp=[],
                                   started_epoch=None, ended_epoch=None)
        self.assertEqual(result["series"], [])
        self.assertTrue(any(n["code"] == "no_timeline" for n in result["notes"]))

    def test_series_counts_concurrent_occupancy(self) -> None:
        result = build_utilization(
            solvers=[_solver(0, 10), _solver(5, 15)],
            timers=[_timer(20, 5)],
            ramp=[_ramp(0, 0), _ramp(30, 2), _ramp(40, 0)],
            started_epoch=T0, ended_epoch=T0 + 45,
        )
        by_t = {row["t"]: row for row in result["series"]}
        self.assertEqual(by_t[7]["solving"], 2, "overlapping solves were not both counted")
        self.assertEqual(by_t[12]["solving"], 1)
        self.assertEqual(by_t[22]["timer_waiting"], 1)
        self.assertEqual(by_t[35]["streaming"], 2, "stream step function did not hold its value")
        self.assertEqual(by_t[42]["streaming"], 0)

    def test_summary_reports_peaks_and_means(self) -> None:
        result = build_utilization(
            solvers=[_solver(0, 10)], timers=[],
            ramp=[_ramp(0, 1), _ramp(10, 0)],
            started_epoch=T0, ended_epoch=T0 + 20,
        )
        summary = result["summary"]
        self.assertEqual(summary["peak_solving"], 1)
        self.assertEqual(summary["peak_streaming"], 1)
        self.assertEqual(summary["solver_lane_capacity"], 3)
        self.assertGreater(summary["window_seconds"], 0)

    def test_missing_evidence_is_noted_never_silently_zero(self) -> None:
        result = build_utilization(
            solvers=[{"task_id": "a"}], timers=[{"task_id": "a"}],
            ramp=[{"host": "h"}],
            started_epoch=T0, ended_epoch=T0 + 5,
        )
        codes = {note["code"] for note in result["notes"]}
        self.assertEqual(codes, {"solver_spans_missing", "timer_spans_missing", "stream_steps_missing"})


class ConstraintAttributionTests(unittest.TestCase):
    def test_single_stream_with_many_parts_blames_stream_admission(self) -> None:
        result = build_utilization(
            solvers=[_solver(0, 30)], timers=[],
            ramp=[_ramp(0, 1), _ramp(40, 1)],
            started_epoch=T0, ended_epoch=T0 + 60, ready_tasks=5,
        )
        self.assertEqual(result["constraint"]["layer"], "stream_admission")
        self.assertIn("5", result["constraint"]["detail"])

    def test_single_part_is_not_blamed_on_admission(self) -> None:
        """One part can never demonstrate a concurrency ceiling."""
        result = build_utilization(
            solvers=[], timers=[], ramp=[_ramp(0, 1), _ramp(10, 0)],
            started_epoch=T0, ended_epoch=T0 + 20, ready_tasks=1,
        )
        self.assertEqual(result["constraint"]["layer"], "insufficient_demand")

    def test_real_overlap_is_not_reported_as_a_constraint(self) -> None:
        result = build_utilization(
            solvers=[], timers=[],
            ramp=[_ramp(0, 3), _ramp(50, 0)],
            started_epoch=T0, ended_epoch=T0 + 60, ready_tasks=4,
        )
        self.assertEqual(result["constraint"]["layer"], "none_conclusive")


if __name__ == "__main__":
    unittest.main()
