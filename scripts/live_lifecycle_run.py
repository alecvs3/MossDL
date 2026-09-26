"""Single-command live end-to-end lifecycle runner.

Examples:
    # Discover a >=3-part DataNodes package on the examplepack home page and run it.
    python scripts/live_lifecycle_run.py --package Kristala --interactive

    # Pick the smallest >=3-part package automatically.
    python scripts/live_lifecycle_run.py

    # Only discover packages (no download).
    python scripts/live_lifecycle_run.py --intake-only

    # Reuse a JSON run spec.
    python scripts/live_lifecycle_run.py --spec tests/live/run_specs/examplepack_kristala.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.live.live_harness import LinkIntake, LiveRun, preflight

WIDTH = 96


def _mb(value: object) -> str:
    return f"{float(value) / 1_048_576:,.0f} MB" if isinstance(value, (int, float)) else "-"


def _num(value: object, spec: str = ".2f") -> str:
    return format(value, spec) if isinstance(value, (int, float)) else "-"


def _section(title: str) -> None:
    print("\n" + title)
    print("-" * WIDTH)


def print_report_table(report: dict) -> None:
    """Human-readable end-of-run digest of ``report.json``."""
    run = report.get("run") or {}
    print("\n" + "=" * WIDTH)
    print(f"LIVE RUN  {run.get('package') or '(unknown package)'}")
    print(f"  parts={run.get('parts')}  hoster={run.get('hoster')}  dest={run.get('destination')}")
    print(f"  started={run.get('started')}  wall={_num(run.get('wall_seconds'), '.1f')}s")

    _section("STAGE TIMINGS (union of per-task intervals)")
    print(f"  {'stage':<28}{'seconds':>10}  {'tasks':>5}  started")
    for stage, info in sorted((report.get("stages") or {}).items(),
                              key=lambda kv: -(kv[1].get("seconds") or 0)):
        print(f"  {stage:<28}{_num(info.get('seconds'), '10.2f')}  "
              f"{len(info.get('task_ids') or []):>5}  {info.get('started')}")

    _section("PER-PART THROUGHPUT")
    print(f"  {'task':<9}{'part':<26}{'size':>12}{'secs':>8}{'avg':>8}{'peak':>8}{'p95':>8}{'stalls':>7}")
    for task in report.get("per_task") or []:
        name = (task.get("name") or task.get("task_id") or "")[-25:]
        print(f"  {str(task.get('task_id'))[:8]:<9}{name:<26}{_mb(task.get('size')):>12}"
              f"{_num(task.get('seconds'), '8.1f')}{_num(task.get('mb_s_avg'), '8.2f')}"
              f"{_num(task.get('mb_s_peak'), '8.2f')}{_num(task.get('mb_s_p95'), '8.2f')}"
              f"{str(task.get('stalls') if task.get('stalls') is not None else '-'):>7}")

    solvers = report.get("solvers") or []
    _section(f"CAPTCHA / SOLVERS ({len(solvers)} challenge(s))")
    print(f"  {'task':<9}{'track':<10}{'solver':<12}{'outcome':<11}{'ms':>10}  at")
    for entry in solvers:
        exact = "" if (entry.get("duration_exact") or entry.get("duration_ms") is None) else "~"
        print(f"  {str(entry.get('task_id') or '?')[:8]:<9}{str(entry.get('track') or ''):<10}"
              f"{str(entry.get('solver') or '-'):<12}{str(entry.get('outcome')):<11}"
              f"{exact + _num(entry.get('duration_ms'), '.0f'):>10}  "
              f"{entry.get('challenge_at') or entry.get('solved_at')}")

    timers = [t for t in report.get("timers") or [] if (t.get("countdown_seconds") or 0) > 0]
    _section(f"WAIT TIMERS ({len(timers)} armed of {len(report.get('timers') or [])} timer events)")
    print(f"  {'task':<9}{'marker':<18}{'source':<20}{'countdown':>10}{'waited':>9}")
    for timer in timers:
        print(f"  {str(timer.get('task_id'))[:8]:<9}{str(timer.get('marker')):<18}"
              f"{str(timer.get('source'))[:19]:<20}{_num(timer.get('countdown_seconds'), '10.0f')}"
              f"{_num(timer.get('waited_seconds'), '9.1f')}")

    ramp = report.get("concurrency_ramp") or []
    _section(f"CONCURRENCY RAMP ({len(ramp)} event(s))")
    print(f"  {'at':<22}{'host':<24}{'event':<28}{'active':>7}{'ceil':>6}")
    for step in ramp:
        print(f"  {str(step.get('at')):<22}{str(step.get('host'))[:23]:<24}"
              f"{str(step.get('event'))[:27]:<28}{str(step.get('active_streams')):>7}"
              f"{str(step.get('ceiling') if step.get('ceiling') is not None else '-'):>6}")

    resolve = report.get("resolve_phase") or {}
    if resolve.get("summary"):
        from tests.live.resolve_phase import format_resolve_phase
        _section("RESOLVE PHASE (queued -> direct link)")
        for line in format_resolve_phase(resolve):
            print(line)

    util = report.get("utilization") or {}
    summary = util.get("summary") or {}
    if summary:
        _section("PIPELINE UTILIZATION (per-second occupancy)")
        lanes = summary.get("solver_lane_capacity") or 0
        print(f"  {'layer':<22}{'peak':>6}{'mean':>8}{'capacity':>10}")
        print(f"  {'captcha solving':<22}{summary.get('peak_solving', 0):>6}"
              f"{_num(summary.get('mean_solving'), '8.2f')}{lanes:>10}")
        print(f"  {'wait timers':<22}{summary.get('peak_timer_waiting', 0):>6}"
              f"{_num(summary.get('mean_timer_waiting'), '8.2f')}{'-':>10}")
        print(f"  {'download streams':<22}{summary.get('peak_streaming', 0):>6}"
              f"{_num(summary.get('mean_streaming'), '8.2f')}{'host ceiling':>10}")
        window = summary.get("window_seconds") or 0
        print(f"  window {window}s | streaming {summary.get('seconds_streaming', 0)}s"
              f" | >1 stream {summary.get('seconds_with_multiple_streams', 0)}s"
              f" | <=1 stream with work pending"
              f" {summary.get('seconds_single_stream_while_other_work_pending', 0)}s")
        constraint = util.get("constraint") or {}
        if constraint:
            print(f"  BINDING CONSTRAINT: {constraint.get('layer')} -- {constraint.get('detail')}")

    _section("ARCHIVE JOBS")
    for job in report.get("archive_jobs") or []:
        detail = job.get("stage_detail") or {}
        print(f"  {str(job.get('id'))[:8]}  {str(job.get('state')):<12}"
              f"{str(detail.get('operation')):<10}{_num(job.get('seconds'), '8.2f')}s"
              f"  {detail.get('error') or ''}")
    if not report.get("archive_jobs"):
        print("  (none)")

    notes = report.get("notes") or []
    if notes:
        _section(f"PARSER NOTES ({len(notes)})")
        for note in notes:
            print(f"  {note.get('code')}: {note.get('reason')}")

    _section("ASSERTIONS")
    for check in report.get("assertions") or []:
        print(f"  [{'PASS' if check.get('passed') else 'FAIL'}] {str(check.get('id')):<26}"
              f"{str(check.get('detail'))[:52]}")
    print("=" * WIDTH)
    print(f"VERDICT: {str(report.get('verdict', 'unknown')).upper()}")


def _intake_only(spec: dict) -> int:
    intake = LinkIntake(spec.get("home", "https://example-repacks.test/"))
    html = intake.fetch()
    packages = [p for p in intake.discover(html) if p.part_count >= spec.get("min_parts", 3) and p.contiguous()]
    packages.sort(key=lambda p: p.part_count)
    print(f"Discovered {len(packages)} contiguous DataNodes packages with >= {spec.get('min_parts', 3)} parts:")
    for pkg in packages:
        print(f"  parts={pkg.part_count:3d}  {pkg.name}")
        for number, url in pkg.ordered():
            print(f"      part{number}: {url}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live examplepack -> DataNodes -> download -> unrar runner")
    parser.add_argument("--spec", type=Path, default=None, help="JSON run spec")
    parser.add_argument("--package", default=None, help="substring of the package name to select")
    parser.add_argument("--home", default="https://example-repacks.test/")
    parser.add_argument("--hoster", default="datanodes", choices=["datanodes", "fuckingfast"],
                        help="file hoster to target")
    parser.add_argument("--dest", default=str(Path.home() / "Downloads"), help="download destination directory")
    parser.add_argument("--min-parts", type=int, default=3)
    parser.add_argument("--max-parts", type=int, default=None, help="only fetch the first N parts")
    parser.add_argument("--timeout", type=float, default=3600.0, help="overall run timeout in seconds")
    parser.add_argument("--interactive", action="store_true", help="allow manual CAPTCHA fallback")
    parser.add_argument("--no-seed-real", action="store_true",
                        help="do not load the running app's persisted clearance/settings")
    parser.add_argument("--intake-only", action="store_true", help="only discover packages")
    parser.add_argument("--check", action="store_true", help="preflight only")
    args = parser.parse_args()

    spec: dict = {}
    if args.spec:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    spec.setdefault("home", args.home)
    spec.setdefault("min_parts", args.min_parts)
    spec["package"] = args.package or spec.get("package")
    spec["hoster"] = args.hoster or spec.get("hoster", "datanodes")
    spec["dest"] = args.dest or spec.get("dest")
    spec["max_parts"] = args.max_parts if args.max_parts is not None else spec.get("max_parts")
    spec["interactive"] = args.interactive or bool(spec.get("interactive"))
    spec["timeout"] = args.timeout

    checks = preflight()
    print("preflight:", json.dumps(checks))
    if not all(checks.values()):
        print("preflight failed: missing required tools", file=sys.stderr)
        return 2
    if args.check:
        return 0
    if args.intake_only:
        return _intake_only(spec)

    run = LiveRun(
        destination=Path(spec["dest"]),
        package_name=spec.get("package"),
        min_parts=int(spec.get("min_parts", 3)),
        max_parts=spec.get("max_parts"),
        home=spec["home"],
        hoster=spec.get("hoster", "datanodes"),
        seed_real_profile=not args.no_seed_real,
        interactive=bool(spec.get("interactive")),
        timeout_seconds=float(spec.get("timeout", 3600.0)),
    )
    result = run.run()

    report = result.get("report")
    if not report:
        print("no report.json was produced (run aborted before the report stage)", file=sys.stderr)
        return 1
    print_report_table(report)
    print(f"RUN DIR: {result.get('run_dir')}")
    print(f"REPORT:  {result.get('report_path')}")
    print(f"SOLVES:  {result.get('solves')}")
    print(f"FILES:   {result.get('destination_files')}")
    return 0 if report.get("verdict") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
