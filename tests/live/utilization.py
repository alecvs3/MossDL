"""Per-second occupancy of each pipeline layer, and which one is the constraint.

The pipeline has four independent capacity layers, each with its own cap and no
shared governor:

    CAPTCHA solves   3 lanes, 1.2s launch stagger   (engine/browser_solver.py)
    wait timers      unlimited; 1 POST per host     (engine/timer_scheduler.py)
    download streams per-host calibrated ceiling    (engine/storage_concurrency.py)
    chunks per file  starts at 2, ramps             (engine/limits.py)

Reading a run's totals cannot tell you which of those starved the others. This
builds a one-second grid of how many of each were actually busy, so "capped at
one download" can be attributed to a layer instead of guessed at.

Everything here is derived from engine telemetry that already exists; nothing is
inferred when the evidence is absent -- gaps are reported as notes.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

# Capacities the engine actually enforces, for occupancy ratios.
SOLVER_LANES = 3


def _intervals_from_solvers(solvers: Sequence[Mapping[str, Any]]) -> list[tuple[float, float]]:
    """Solve occupancy: challenge raised -> solved."""
    spans: list[tuple[float, float]] = []
    for row in solvers:
        start, end = row.get("_challenge_epoch"), row.get("_solved_epoch")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
            spans.append((float(start), float(end)))
    return spans


def _intervals_from_timers(timers: Sequence[Mapping[str, Any]]) -> list[tuple[float, float]]:
    """Timer occupancy: detected/armed -> armed + seconds actually waited."""
    spans: list[tuple[float, float]] = []
    for row in timers:
        start = row.get("_detected_epoch")
        waited = row.get("waited_seconds")
        if isinstance(start, (int, float)) and isinstance(waited, (int, float)) and waited > 0:
            spans.append((float(start), float(start) + float(waited)))
    return spans


def _stream_steps(ramp: Sequence[Mapping[str, Any]]) -> list[tuple[float, int]]:
    """Stream occupancy as the engine's own step function of active_streams."""
    steps: list[tuple[float, int]] = []
    for row in ramp:
        at, active = row.get("_epoch"), row.get("active_streams")
        if isinstance(at, (int, float)) and isinstance(active, int):
            steps.append((float(at), int(active)))
    steps.sort(key=lambda item: item[0])
    return steps


def _count_at(spans: Iterable[tuple[float, float]], moment: float) -> int:
    return sum(1 for start, end in spans if start <= moment <= end)


def _step_at(steps: Sequence[tuple[float, int]], moment: float) -> int:
    value = 0
    for at, active in steps:
        if at > moment:
            break
        value = active
    return value


def build_utilization(
    *,
    solvers: Sequence[Mapping[str, Any]],
    timers: Sequence[Mapping[str, Any]],
    ramp: Sequence[Mapping[str, Any]],
    started_epoch: float | None,
    ended_epoch: float | None,
    ready_tasks: int = 0,
) -> dict[str, Any]:
    """Build the per-second series, peaks, and a constraint attribution."""
    notes: list[dict[str, Any]] = []
    solve_spans = _intervals_from_solvers(solvers)
    timer_spans = _intervals_from_timers(timers)
    steps = _stream_steps(ramp)

    if not solve_spans and solvers:
        notes.append({"code": "solver_spans_missing",
                      "reason": "solver rows carried no usable epochs; solve occupancy omitted"})
    if not timer_spans and timers:
        notes.append({"code": "timer_spans_missing",
                      "reason": "timer rows carried no armed epoch or wait duration"})
    if not steps and ramp:
        notes.append({"code": "stream_steps_missing",
                      "reason": "concurrency ramp rows carried no active_streams count"})

    candidates = [value for span in solve_spans + timer_spans for value in span]
    candidates += [at for at, _ in steps]
    if started_epoch is not None:
        candidates.append(float(started_epoch))
    if ended_epoch is not None:
        candidates.append(float(ended_epoch))
    if not candidates:
        return {"series": [], "summary": {}, "constraint": None,
                "notes": notes + [{"code": "no_timeline", "reason": "no epochs to build a grid from"}]}

    start, end = min(candidates), max(candidates)
    span_seconds = max(0, int(end - start))
    if span_seconds > 7200:  # keep the artifact bounded on very long runs
        notes.append({"code": "series_truncated",
                      "reason": f"run spanned {span_seconds}s; series capped at 7200 samples"})
        span_seconds = 7200

    series: list[dict[str, Any]] = []
    for offset in range(span_seconds + 1):
        moment = start + offset
        series.append({
            "t": offset,
            "solving": _count_at(solve_spans, moment),
            "timer_waiting": _count_at(timer_spans, moment),
            "streaming": _step_at(steps, moment),
        })

    def _peak(key: str) -> int:
        return max((row[key] for row in series), default=0)

    def _mean(key: str) -> float:
        return round(sum(row[key] for row in series) / len(series), 3) if series else 0.0

    busy_seconds = sum(1 for row in series if row["streaming"] > 0)
    multi_stream_seconds = sum(1 for row in series if row["streaming"] > 1)
    idle_with_work = sum(
        1 for row in series
        if row["streaming"] <= 1 and (row["solving"] > 0 or row["timer_waiting"] > 0)
    )

    summary = {
        "window_seconds": span_seconds,
        "peak_solving": _peak("solving"),
        "peak_timer_waiting": _peak("timer_waiting"),
        "peak_streaming": _peak("streaming"),
        "mean_solving": _mean("solving"),
        "mean_timer_waiting": _mean("timer_waiting"),
        "mean_streaming": _mean("streaming"),
        "solver_lane_capacity": SOLVER_LANES,
        "solver_lane_occupancy": round(_mean("solving") / SOLVER_LANES, 3) if SOLVER_LANES else None,
        "seconds_streaming": busy_seconds,
        "seconds_with_multiple_streams": multi_stream_seconds,
        "seconds_single_stream_while_other_work_pending": idle_with_work,
    }

    constraint = _attribute_constraint(summary, ready_tasks)
    return {"series": series, "summary": summary, "constraint": constraint, "notes": notes}


def _attribute_constraint(summary: Mapping[str, Any], ready_tasks: int) -> dict[str, Any]:
    """Name the binding layer from the evidence, or admit it is unclear."""
    peak_streaming = int(summary.get("peak_streaming") or 0)
    peak_solving = int(summary.get("peak_solving") or 0)
    multi = int(summary.get("seconds_with_multiple_streams") or 0)

    if peak_streaming <= 1 and ready_tasks > 1:
        return {
            "layer": "stream_admission",
            "detail": (
                f"never exceeded 1 concurrent stream across the whole run while {ready_tasks} "
                f"parts were in play; the storage-host ceiling is the binding constraint, "
                f"not solving or timers"
            ),
        }
    if peak_streaming <= 1:
        return {"layer": "insufficient_demand",
                "detail": "only one part was ever ready to stream; concurrency was never exercised"}
    if peak_solving >= SOLVER_LANES and multi < int(summary.get("seconds_streaming") or 0) / 2:
        return {"layer": "solver_lanes",
                "detail": (f"solve lanes hit their cap of {SOLVER_LANES} while streams were mostly "
                           f"single; parts arrive at the transport one at a time")}
    return {"layer": "none_conclusive",
            "detail": (f"peak {peak_streaming} streams with {multi}s of genuine overlap; "
                       f"no single layer was pinned for the whole run")}
