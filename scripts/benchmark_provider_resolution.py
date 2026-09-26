"""Run a provider package through the real engine and capture its critical path."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.service import EngineService  # noqa: E402


def _load_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.url or [])
    if args.urls_file:
        urls.extend(line.strip() for line in args.urls_file.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#"))
    if not urls:
        raise SystemExit("provide at least one --url or --urls-file")
    return urls


def _fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def run(args: argparse.Namespace, service: EngineService | None = None) -> dict:
    urls = _load_urls(args)
    run_stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    run_index = int(getattr(args, "run_index", 1))
    artifact_dir = (args.output_dir / f"{run_stamp}-{args.profile}-{args.mode}-r{run_index:02d}").resolve()
    artifact_dir.mkdir(parents=True, exist_ok=False)
    owns_service = service is None
    if service is None:
        service = EngineService(artifact_dir / "engine-data")
    task_ids: list[str] = []
    solve_started: set[str] = set()
    started = time.perf_counter()
    trace_result: dict = {}
    outcome = "timeout"
    try:
        service.dispatch("pause_engine")
        service.dispatch("critical_trace_start", {
            "output_dir": str(artifact_dir / "traces"),
            "label": args.label,
            "profile": args.profile,
            "mode": args.mode,
        })
        for url in urls:
            leaf = unquote(Path(urlsplit(url).path).name) or "download"
            task = service.dispatch("add_task", {
                "url": url,
                "destination": str(artifact_dir / "payload"),
                "display_name": leaf,
                "browser_context": {"diagnostic_resolution_only": True},
            })
            task_ids.append(task["id"])
        service.dispatch("resume_engine")
        for task_id in task_ids:
            service.dispatch("download_task", {"id": task_id})

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            tasks = [service.store.get(task_id) for task_id in task_ids]
            tasks = [task for task in tasks if task is not None]
            for task in tasks:
                if task.state == "needs_user" and args.solve_challenges:
                    package = task.package_key or task.id
                    challenge = task.user_challenge or {}
                    challenge_id = str(challenge.get("challenge_id") or "")
                    solve_key = f"{package}:{challenge_id}"
                    if solve_key not in solve_started:
                        solve_started.add(solve_key)
                        service.dispatch("solve_multipart_captcha", {
                            "group_id": package,
                            "host": (urlsplit(task.source_url).hostname or "").lower(),
                            "page_url": challenge.get("page_url") or task.source_url,
                        })
            # Direct URLs are deliberately not rehydrated from the task store.
            # The diagnostic hold is written only after the live in-memory plan
            # has direct URLs and real preflight succeeds.
            direct_ready = [task for task in tasks if
                            task.state == "preflight"
                            and task.paused_reason == "Resolution benchmark stopped after real preflight"]
            if len(direct_ready) == len(task_ids):
                outcome = "all_direct_urls_acquired"
                break
            if tasks and all(task.state in {"failed", "canceled", "completed"} for task in tasks):
                outcome = "terminal_before_all_resolved"
                break
            time.sleep(args.poll_interval)
    finally:
        try:
            trace_result = service.dispatch("critical_trace_stop")
        finally:
            for task_id in task_ids:
                task = service.store.get(task_id)
                if task and task.state not in {"completed", "canceled"}:
                    try:
                        service.dispatch("cancel_task", {"id": task_id})
                    except Exception:
                        pass
            snapshots = [service.store.get(task_id) for task_id in task_ids]
            if owns_service:
                service.close()

    report = {
        "schema": 1,
        "outcome": outcome,
        "wall_seconds": round(time.perf_counter() - started, 6),
        "profile": args.profile,
        "mode": args.mode,
        "inputs": [{
            "fingerprint": _fingerprint(url),
            "host": (urlsplit(url).hostname or "").lower(),
            "part": index + 1,
        } for index, url in enumerate(urls)],
        "tasks": [{
            "id": task.id,
            "state": task.state,
            "package_id": task.package_key,
            "part_number": task.package_part_number,
            "resolved_items": len(task.resolved),
            "direct_items": sum(1 for item in task.resolved if item.direct_url),
            "error_type": type(task.error).__name__ if task.error else None,
        } for task in snapshots if task is not None],
        "challenge_solves_started": len(solve_started),
        "trace": trace_result,
    }
    report_path = artifact_dir / "benchmark.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append")
    parser.add_argument("--urls-file", type=Path)
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT / "artifacts" / "benchmarks" / "provider-resolution")
    parser.add_argument("--profile", choices=("cold", "warm", "challenge"), default="cold")
    parser.add_argument("--mode", choices=("engine-multipart",), default="engine-multipart")
    parser.add_argument("--label", default="provider-resolution")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--poll-interval", type=float, default=0.05)
    parser.add_argument("--repeat", type=int, default=1,
                        help="run the profile repeatedly and report median/range")
    parser.add_argument("--no-solve-challenges", dest="solve_challenges", action="store_false")
    parser.set_defaults(solve_challenges=True)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    reports: list[dict] = []
    shared_service: EngineService | None = None
    batch_stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        # Warm means the same engine, HTTP pools, provider workers, browser actor,
        # and host-session cache survive all repetitions. Cold/challenge runs
        # isolate engine state and explicitly reap the browser between samples.
        if args.profile == "warm" and args.repeat > 1:
            shared_service = EngineService(args.output_dir / f"{batch_stamp}-warm-shared-state")
        for index in range(1, args.repeat + 1):
            args.run_index = index
            if args.profile in {"cold", "challenge"}:
                from engine.browser_solver import solver_daemon
                solver_daemon.reap()
            reports.append(run(args, shared_service))
    finally:
        if shared_service is not None:
            shared_service.close()
    walls = [float(report["wall_seconds"]) for report in reports]
    summary = {
        "schema": 1,
        "profile": args.profile,
        "runs": len(reports),
        "successful_runs": sum(report["outcome"] == "all_direct_urls_acquired" for report in reports),
        "wall_seconds": {
            "median": round(statistics.median(walls), 6),
            "min": round(min(walls), 6),
            "max": round(max(walls), 6),
            "range": round(max(walls) - min(walls), 6),
        },
        "challenge_solves": [report["challenge_solves_started"] for report in reports],
        "reports": [report["report_path"] for report in reports],
    }
    summary_path = args.output_dir / f"{batch_stamp}-{args.profile}-{args.mode}-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path.resolve())
    print(json.dumps(summary if args.repeat > 1 else reports[0], indent=2))
    return 0 if summary["successful_runs"] == len(reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
