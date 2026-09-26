"""Empirical Analysis Command Line Interface.

Provides a unified command center to inspect log locations, audit telemetry,
and execute end-to-end multi-part lifecycle tests.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import DEFAULT_DESTINATION, get_log_paths
from .lifecycle_runner import LifecycleRunner
from .log_analyzer import audit_logs, format_report_summary, read_engine_logs


def cmd_show_locations() -> int:
    """Print all diagnostic log locations for rapid access."""
    paths = get_log_paths()
    print("=================================================================")
    print(" Transfer Manager — Diagnostic Log Locations")
    print("=================================================================")
    print(f"Engine Daemon JSONL:       {paths.engine_jsonl}")
    print(f"  Exists:                  {paths.engine_jsonl.exists()}")
    print(f"TaskStore SQLite DB:       {paths.task_store_db}")
    print(f"  Exists:                  {paths.task_store_db.exists()}")
    print(f"Download Session Logs:     {paths.download_logs_dir}")
    print(f"  Exists:                  {paths.download_logs_dir.exists()}")
    print(f"Host Concurrency Audit:    {paths.host_concurrency_audit}")
    print(f"  Exists:                  {paths.host_concurrency_audit.exists()}")
    print("=================================================================")
    return 0


def cmd_analyze_logs(limit: int) -> int:
    """Audit the latest records from engine.jsonl."""
    records = read_engine_logs(limit=limit)
    if not records:
        print("No engine log records found or file does not exist.")
        return 1
    report = audit_logs(records)
    print(format_report_summary(report))
    return 0 if not report.exceptions else 1


def cmd_test_part(url: str, destination: Path, timeout: float) -> int:
    """Execute a single-part live lifecycle test."""
    print(f"Starting single-part lifecycle test for:\n  URL: {url}\n  Target: {destination}")
    runner = LifecycleRunner(destination=destination)

    def on_progress(msg: str, pct: float) -> None:
        print(f"  [{pct:5.1f}%] {msg}")

    result = asyncio.run(runner.run_single_part_test(url, timeout_seconds=timeout, progress_cb=on_progress))
    print("\n--- Test Result ---")
    print(f"State:        {result.state}")
    print(f"Duration:     {result.duration_s}s")
    print(f"File:         {result.file_path} ({result.file_size:,} bytes)")
    if result.solver_used:
        print(f"Solver:       {result.solver_used}")
    if result.error:
        print(f"Error:        {result.error}")
        return 1
    return 0 if result.state == "completed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Transfer Manager Empirical Analysis Suite")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # show-locations
    subparsers.add_parser("show-locations", help="Show all diagnostic log locations")

    # analyze-logs
    p_analyze = subparsers.add_parser("analyze-logs", help="Audit engine telemetry logs")
    p_analyze.add_argument("--limit", type=int, default=500, help="Number of recent records to audit")

    # test-part
    p_part = subparsers.add_parser("test-part", help="Run live single-part lifecycle test")
    p_part.add_argument("url", help="Direct host or file URL")
    p_part.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION, help="Download directory")
    p_part.add_argument("--timeout", type=float, default=120.0, help="Test timeout in seconds")

    args = parser.parse_args()

    if args.command == "show-locations":
        return cmd_show_locations()
    elif args.command == "analyze-logs":
        return cmd_analyze_logs(limit=args.limit)
    elif args.command == "test-part":
        return cmd_test_part(args.url, args.destination, args.timeout)

    return 0


if __name__ == "__main__":
    sys.exit(main())
