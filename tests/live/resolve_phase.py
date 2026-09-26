"""Resolve-phase benchmark: how long a package takes to become downloadable.

For a multipart package the transport is often not the cost -- getting every part
from "queued" to `direct_link_acquired` is. That phase is CAPTCHA solving, timer
waits and metadata resolution, and it overlaps across parts to a degree nothing
previously measured.

Baseline captured 2026-09-17 (StarCraft_Remastered, 5 parts, DataNodes):

    per-part   48.5  51.4  60.2  71.9  96.9 s
    wall span  153.5 s     naive sum 328.9 s     overlap 2.14x
    churn      21, 11, 11, 11, 10 stage transitions per part

`overlap` is the honest measure of parallelism: naive sum / wall span. 1.0 means
strictly serial; N means N parts resolved concurrently on average. The floor for
wall span is the slowest single part, so that is reported too -- it is what
perfect parallelism would achieve.

`churn` counts lifecycle transitions during the resolve phase. A part should need
roughly four (resolving -> challenge -> solving -> direct link); more means
something is rewriting the task's stage that should not be.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# Stages that belong to the resolve phase, used for the churn count.
RESOLVE_STAGES = frozenset({
    "resolving_metadata",
    "captcha_challenge_detected",
    "captcha_solving",
    "captcha_verifying",
    "hoster_wait_timer",
})
# A clean resolve: enter resolving, hit a challenge, solve it, get the link.
EXPECTED_CHURN = 4


def build_resolve_phase(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarise the resolve phase from lifecycle telemetry records.

    Each record is a telemetry entry whose ``context`` carries ``stage ==
    "lifecycle"``, a ``task_id``, a ``to_stage`` and an ``entered_at_us`` epoch.
    """
    first_seen: dict[str, float] = {}
    direct_at: dict[str, float] = {}
    churn: dict[str, int] = {}
    solver_launches = 0
    challenges = 0

    for record in records:
        message = str(record.get("message") or "")
        if "SOLVER_LAUNCHED" in message:
            solver_launches += 1
        if "[CAPTCHA_CHALLENGE]" in message:
            challenges += 1
        context = record.get("context") or {}
        if context.get("stage") != "lifecycle":
            continue
        stage = context.get("to_stage")
        task_id = str(context.get("task_id") or "")
        if not stage or not task_id:
            continue
        try:
            entered = float(context.get("entered_at_us") or 0.0)
        except (TypeError, ValueError):
            continue
        if not entered:
            continue
        first_seen.setdefault(task_id, entered)
        if stage == "direct_link_acquired":
            direct_at.setdefault(task_id, entered)
        if stage in RESOLVE_STAGES:
            churn[task_id] = churn.get(task_id, 0) + 1

    per_task = []
    for task_id, reached in direct_at.items():
        started = first_seen.get(task_id)
        if started is None or reached < started:
            continue
        per_task.append({
            "task_id": task_id,
            "seconds": round(reached - started, 3),
            "stage_transitions": churn.get(task_id, 0),
        })
    per_task.sort(key=lambda row: row["seconds"])

    if not per_task:
        return {"per_task": [], "summary": {},
                "notes": [{"code": "no_resolved_parts",
                           "reason": "no part reached direct_link_acquired; nothing to measure"}]}

    durations = [row["seconds"] for row in per_task]
    starts = [first_seen[row["task_id"]] for row in per_task]
    ends = [direct_at[row["task_id"]] for row in per_task]
    wall = max(ends) - min(starts)
    naive = sum(durations)
    transitions = [row["stage_transitions"] for row in per_task]

    summary = {
        "parts": len(per_task),
        "mean_seconds": round(naive / len(per_task), 3),
        "max_seconds": round(max(durations), 3),
        "min_seconds": round(min(durations), 3),
        "wall_seconds": round(wall, 3),
        "naive_sum_seconds": round(naive, 3),
        # >1 means parts genuinely overlapped; 1.0 means strictly serial.
        "overlap_factor": round(naive / wall, 3) if wall > 0 else None,
        # What perfect parallelism would achieve: the slowest single part.
        "ideal_wall_seconds": round(max(durations), 3),
        "parallel_efficiency": round(max(durations) / wall, 3) if wall > 0 else None,
        "stage_transitions_total": sum(transitions),
        "stage_transitions_max": max(transitions),
        "expected_transitions_per_part": EXPECTED_CHURN,
        "excess_transitions": max(0, sum(transitions) - EXPECTED_CHURN * len(per_task)),
        "challenges": challenges,
        "solver_launches": solver_launches,
    }
    return {"per_task": per_task, "summary": summary, "notes": []}


def format_resolve_phase(block: Mapping[str, Any]) -> list[str]:
    """Render the resolve phase for the run table."""
    summary = block.get("summary") or {}
    if not summary:
        return ["  (no part reached a direct link)"]
    lines = [
        f"  {'parts':<26}{summary.get('parts')}",
        f"  {'per-part mean / max':<26}{summary.get('mean_seconds')}s / {summary.get('max_seconds')}s",
        f"  {'wall span':<26}{summary.get('wall_seconds')}s",
        f"  {'naive sum (no overlap)':<26}{summary.get('naive_sum_seconds')}s",
        f"  {'overlap factor':<26}{summary.get('overlap_factor')}x",
        f"  {'ideal wall (perfect ||)':<26}{summary.get('ideal_wall_seconds')}s "
        f"(efficiency {summary.get('parallel_efficiency')})",
        f"  {'stage transitions':<26}{summary.get('stage_transitions_total')} total, "
        f"max {summary.get('stage_transitions_max')}/part "
        f"(expected ~{summary.get('expected_transitions_per_part')}/part, "
        f"excess {summary.get('excess_transitions')})",
        f"  {'challenges / launches':<26}{summary.get('challenges')} / {summary.get('solver_launches')}",
    ]
    return lines
