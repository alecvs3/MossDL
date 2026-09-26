"""Run the deterministic full-lifecycle edge-case matrix in one command.

Usage:
    python scripts/run_lifecycle_matrix.py            # every non-deep scenario
    python scripts/run_lifecycle_matrix.py --scenario multipart_shared_captcha
    TRANSFER_MANAGER_DEEP=1 python scripts/run_lifecycle_matrix.py   # + deep faults

Covers the happy-path matrix plus the wave-5 fault injections (429/Retry-After
backpressure, a missing archive volume, and a transient WinError 32 during
source cleanup); fault evidence is printed per scenario and persisted in each
scenario's ``summary.json``.

Runs the real ``EngineService`` through scripted seams (no live network),
writes a full telemetry timeline per scenario to
``.test-artifacts/lifecycle-matrix/<scenario>/telemetry.jsonl``, prints a
per-scenario pass/fail summary, and exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.e2e.lifecycle_harness import (
    Scenario,
    deep_enabled,
    default_scenarios,
    fault_failures,
    fault_scenarios,
    run_scenario,
)

ARTIFACT_ROOT = ROOT / ".test-artifacts" / "lifecycle-matrix"


def _verify(scenario: Scenario, result) -> list[str]:
    failures: list[str] = []
    for task_id in result.task_ids:
        state = result.final_states.get(task_id)
        if state not in scenario.expect_states:
            failures.append(f"task {task_id[:8]} ended {state}, expected {scenario.expect_states}")
    if scenario.expect_challenges is not None and result.challenge_count != scenario.expect_challenges:
        failures.append(f"challenges={result.challenge_count}, expected {scenario.expect_challenges}")
    if scenario.expect_timer_stage and not any("hoster_wait_timer" in seq for seq in result.stages.values()):
        failures.append("no hoster_wait_timer stage observed")
    if scenario.expect_archive_completed:
        if not any(j.get("state") == "completed" for j in result.archive_jobs):
            failures.append("no completed archive job")
        if "content.bin" not in result.destination_files:
            failures.append("archive output missing content.bin")
        remaining = [n for n in result.destination_files if n.endswith(".rar")]
        if remaining:
            failures.append(f"source volumes not cleaned: {remaining}")
    for error in result.errors:
        if "Traceback" in error:
            failures.append(f"engine error: {error}")
    failures.extend(fault_failures(scenario, result))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Lifecycle edge-case matrix runner")
    parser.add_argument("--scenario", action="append", default=None,
                        help="run only the named scenario(s); default: all")
    parser.add_argument("--timeline", action="store_true",
                        help="print the full telemetry timeline for each scenario")
    args = parser.parse_args()

    scenarios = default_scenarios() + fault_scenarios()
    if args.scenario:
        wanted = set(args.scenario)
        scenarios = [s for s in scenarios if s.name in wanted]
        if not scenarios:
            print(f"no scenarios matched {sorted(wanted)}")
            return 2
    elif not deep_enabled():
        skipped = [s.name for s in scenarios if s.deep]
        scenarios = [s for s in scenarios if not s.deep]
        if skipped:
            print(f"skipping deep scenarios (set TRANSFER_MANAGER_DEEP=1 to run): {skipped}")

    total_failed = 0
    for scenario in scenarios:
        print("=" * 78)
        print(f"SCENARIO: {scenario.name}")
        print("=" * 78)
        with tempfile.TemporaryDirectory() as temp:
            result = run_scenario(scenario, Path(temp), ARTIFACT_ROOT)
        failures = _verify(scenario, result)

        for task_id in result.task_ids:
            stages = " -> ".join(result.stage_sequence(task_id)) or "(none)"
            print(f"  {task_id[:8]}  state={result.final_states.get(task_id):<10} stages: {stages}")
        print(f"  challenges={result.challenge_count}  solves={result.solve_count}  "
              f"elapsed={result.elapsed_seconds:.2f}s  files={result.destination_files}")
        for job in result.archive_jobs:
            print(f"  archive job {job.get('id', '')[:8]} state={job.get('state')} op={job.get('operation')}")
        for key in ("throttle", "cleanup_lock", "part_deletions", "archive_refusals",
                    "permits_released_during_backoff", "breaker_events"):
            if key in result.fault:
                print(f"  fault.{key}: {result.fault[key]}")
        if "breaker_recovery" in result.fault:
            recovery = result.fault["breaker_recovery"]
            print(f"  fault.breaker_recovery: before={recovery['before'].get('state')} "
                  f"after={recovery['after'].get('state')} "
                  f"ceiling={recovery['after'].get('verified_ceiling')} "
                  f"cooldown_skipped={recovery['cooldown_seconds_skipped']}s")
        print(f"  telemetry: {result.artifact_dir}/telemetry.jsonl")

        if args.timeline:
            for entry in result.telemetry:
                if entry.level in {"INFO", "WARN", "ERROR", "FATAL"}:
                    print(f"    {entry.timestamp} [{entry.level}] {entry.message}")

        if failures:
            total_failed += 1
            print("  RESULT: FAIL")
            for failure in failures:
                print(f"    - {failure}")
        else:
            print("  RESULT: PASS")

    print("=" * 78)
    passed = len(scenarios) - total_failed
    print(f"MATRIX COMPLETE: {passed}/{len(scenarios)} scenarios passed")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
