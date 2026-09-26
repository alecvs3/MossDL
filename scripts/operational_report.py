"""CLI for generating unified, secret-safe operational and benchmark reports."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.benchmarking import BenchmarkRunner
from engine.observability import (
    OperationalAggregator,
    human_operational_summary,
    redact_operational,
)
from engine.service import EngineService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate engine operational & benchmark report")
    parser.add_argument("--fixture", action="store_true", help="run deterministic fixture benchmark")
    parser.add_argument("--task-id", dest="task_id", default=None, help="task ID to correlate with diagnostics")
    parser.add_argument("--output", default=None, help="path to write JSON report")
    parser.add_argument("--format", choices=["json", "text"], default="text", help="stdout display format")
    parser.add_argument("--data-dir", dest="data_dir", default=None, help="engine data directory")
    args = parser.parse_args(argv)

    service = None
    if args.data_dir:
        service = EngineService(args.data_dir)
    elif args.task_id:
        # Default local engine service
        service = EngineService()

    aggregator = OperationalAggregator(service=service)

    bm_report = None
    if args.fixture:
        runner = BenchmarkRunner()
        bm_report = runner.run("all")

    report = aggregator.generate_report(
        task_id=args.task_id,
        benchmark_report=bm_report,
    )
    report_dict = report.to_dict()

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report_dict, indent=2) + "\n", encoding="utf-8")

    if args.format == "json":
        print(json.dumps(report_dict, indent=2))
    else:
        print(human_operational_summary(report))

    if service:
        service.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())