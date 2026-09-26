"""Analyze an engine critical trace without relying on log-message parsing."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def analyze_trace(path: str | Path) -> dict[str, Any]:
    trace_path = Path(path)
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows.sort(key=lambda row: (int(row["t_mono_ns"]), int(row.get("sequence", 0))))
    starts: dict[str, dict[str, Any]] = {}
    durations: dict[str, list[int]] = defaultdict(list)
    completed_spans: list[dict[str, Any]] = []
    task_bounds: dict[str, list[int]] = defaultdict(list)
    package_bounds: dict[str, list[int]] = defaultdict(list)
    pending_queues: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    queue_durations: dict[str, list[int]] = defaultdict(list)
    phases = Counter()
    resources = Counter()
    unmatched_ends = 0
    for row in rows:
        phases[str(row["phase"])] += 1
        if row.get("resource"):
            resources[str(row["resource"])] += 1
        if row.get("task_id"):
            task_bounds[str(row["task_id"])].append(int(row["t_mono_ns"]))
        if row.get("package_id"):
            package_bounds[str(row["package_id"])].append(int(row["t_mono_ns"]))
        resource_key = (
            str(row.get("task_id") or ""),
            str(row.get("resource") or ""),
            str(row.get("resource_id") or ""),
        )
        if str(row.get("phase", "")).endswith(".queued") and row.get("resource"):
            pending_queues[resource_key].append(row)
        span_id = row.get("span_id")
        if row.get("event") == "span_start" and span_id:
            starts[str(span_id)] = row
            if row.get("resource") and pending_queues.get(resource_key):
                queued = pending_queues[resource_key].pop(0)
                queue_durations[str(row["resource"])].append(
                    int(row["t_mono_ns"]) - int(queued["t_mono_ns"])
                )
        elif row.get("event") == "span_end" and span_id:
            started = starts.pop(str(span_id), None)
            if started is None:
                unmatched_ends += 1
            else:
                duration_ns = int(row["t_mono_ns"]) - int(started["t_mono_ns"])
                durations[str(row["phase"])].append(duration_ns)
                completed_spans.append({
                    "phase": str(row["phase"]),
                    "task_id": row.get("task_id"),
                    "package_id": row.get("package_id"),
                    "resource": row.get("resource"),
                    "duration_ns": duration_ns,
                    "start_ns": int(started["t_mono_ns"]),
                    "end_ns": int(row["t_mono_ns"]),
                })
    timeline_ns = int(rows[-1]["t_mono_ns"]) - int(rows[0]["t_mono_ns"]) if len(rows) > 1 else 0
    phase_summary = [{
        "phase": phase,
        "count": len(values),
        "total_us": round(sum(values) / 1_000, 1),
        "max_us": round(max(values) / 1_000, 1),
        "mean_us": round((sum(values) / len(values)) / 1_000, 1),
    } for phase, values in durations.items()]
    phase_summary.sort(key=lambda item: item["total_us"], reverse=True)
    per_task = [{
        "task_id": task_id,
        "wall_us": round((max(values) - min(values)) / 1_000, 1),
        "events": len(values),
    } for task_id, values in task_bounds.items()]
    per_task.sort(key=lambda item: item["wall_us"], reverse=True)
    packages = []
    for package_id, values in package_bounds.items():
        member_tasks = [item for item in per_task
                        if any(row.get("package_id") == package_id and row.get("task_id") == item["task_id"]
                               for row in rows)]
        package_wall_ns = max(values) - min(values)
        member_wall_ns = sum(int(item["wall_us"] * 1_000) for item in member_tasks)
        packages.append({
            "package_id": package_id,
            "wall_us": round(package_wall_ns / 1_000, 1),
            "tasks": len(member_tasks),
            "overlap_factor": round(member_wall_ns / package_wall_ns, 3) if package_wall_ns else None,
        })
    packages.sort(key=lambda item: item["wall_us"], reverse=True)
    queue_summary = [{
        "resource": resource,
        "count": len(values),
        "total_us": round(sum(values) / 1_000, 1),
        "max_us": round(max(values) / 1_000, 1),
        "mean_us": round((sum(values) / len(values)) / 1_000, 1),
    } for resource, values in queue_durations.items()]
    queue_summary.sort(key=lambda item: item["total_us"], reverse=True)
    critical_spans = sorted(completed_spans, key=lambda item: item["duration_ns"], reverse=True)[:20]
    critical_spans = [{**{key: value for key, value in item.items()
                          if key not in {"duration_ns", "start_ns", "end_ns"}},
                       "duration_us": round(item["duration_ns"] / 1_000, 1)}
                      for item in critical_spans]
    duplicate_signals = {
        "step_advance_clicks": phases.get("solver.step_advance.click", 0),
        "step_advance_retries": phases.get("solver.step_advance.retry", 0),
        "final_posts": sum(1 for row in rows
                           if row.get("phase") == "provider.datanodes.final_post"
                           and row.get("event") == "span_start"),
        "turnstile_detections": phases.get("solver.turnstile.detected", 0),
        "turnstile_solves": phases.get("solver.turnstile.solved", 0),
    }
    recommendations = []
    if duplicate_signals["step_advance_retries"]:
        recommendations.append({
            "priority": 1,
            "area": "browser step advancement",
            "evidence": f"{duplicate_signals['step_advance_retries']} first clicks produced no request, DOM, URL, timer, widget, or response acknowledgement before timeout",
            "action": "inspect the provider form handler and request-dispatch trace; retain one bounded retry only when step one remains",
        })
    for item in queue_summary:
        if item["max_us"] >= 250_000:
            recommendations.append({
                "priority": 2,
                "area": f"{item['resource']} admission",
                "evidence": f"maximum queue wait {item['max_us'] / 1_000_000:.3f}s across {item['count']} acquisitions",
                "action": "compare queue capacity with productive work and provider rejection rate before changing the bound",
            })
    recommendations.sort(key=lambda item: item["priority"])
    return {
        "schema": 1,
        "trace": str(trace_path.resolve()),
        "valid": not starts and unmatched_ends == 0,
        "events": len(rows),
        "timeline_us": round(timeline_ns / 1_000, 1),
        "unmatched_starts": len(starts),
        "unmatched_ends": unmatched_ends,
        "phase_counts": dict(phases),
        "resource_counts": dict(resources),
        "phase_durations": phase_summary,
        "queue_durations": queue_summary,
        "critical_spans": critical_spans,
        "duplicate_signals": duplicate_signals,
        "packages": packages,
        "tasks": per_task,
        "recommendations": recommendations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = analyze_trace(args.trace)
    rendered = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
