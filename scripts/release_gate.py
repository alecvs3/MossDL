"""Deterministic release-gate orchestration.

Evaluates:
1. Phase 5 preflight check.
2. Security scan on safe fixtures.
3. Operational report and benchmark suite (custom, rust, aria2).
4. Explicit live canary opt-in policy.
5. Safe pre-transfer and post-progress fallback/rollback rules.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.benchmarking import BenchmarkRunner
from engine.observability import OperationalAggregator, redact_operational
from engine.service import EngineService
from scripts.phase5_preflight import verify_phase5_contracts
from scripts.security_scan import scan_root

SCHEMA_VERSION = 1


def run_release_gate(
    *,
    fixtures_only: bool = True,
    canary_opt_in: bool = False,
    canary_target: str | None = None,
    service: EngineService | None = None,
) -> Dict[str, Any]:
    decision: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "release_gate": "failed",
        "status": "blocked",
        "gates": {},
        "canary": {
            "opt_in": canary_opt_in,
            "target": canary_target,
            "status": "skipped",
        },
        "blockers": [],
    }

    # Gate 1: Phase 5 Preflight
    preflight_passed, preflight_errors = verify_phase5_contracts(service=service)
    decision["gates"]["phase5_preflight"] = {
        "status": "passed" if preflight_passed else "failed",
        "errors": preflight_errors,
    }
    if not preflight_passed:
        decision["blockers"].append("Phase 5 prerequisite contracts missing or failed")
        decision["status"] = "blocked"
        return decision

    # Gate 2: Security Scan on repo fixtures
    safe_fixtures = ROOT / "tests" / "security" / "fixtures" / "secret-free-inputs.json"
    sec_findings = scan_root(safe_fixtures)
    sec_passed = len(sec_findings) == 0
    decision["gates"]["security_scan"] = {
        "status": "passed" if sec_passed else "failed",
        "findings_count": len(sec_findings),
    }
    if not sec_passed:
        decision["blockers"].append(f"Security scanner found {len(sec_findings)} issues in safe fixture")

    # Gate 3: Fixture Benchmark & Operational Observability
    runner = BenchmarkRunner()
    bm_report = runner.run("all")
    bm_data = bm_report.to_dict()
    bm_summary = bm_data.get("summary", {})
    all_bm_passed = bool(bm_summary.get("all_gates_passed", False))

    aggregator = OperationalAggregator(service=service)
    op_report = aggregator.generate_report(benchmark_report=bm_report)
    decision["gates"]["operational_benchmark"] = {
        "status": "passed" if all_bm_passed else "failed",
        "benchmark_summary": bm_summary,
        "metrics": op_report.to_dict()["metrics"],
    }
    if not all_bm_passed:
        decision["blockers"].append("Benchmark fixture release gate failed")

    # Gate 4: Live Canary (Explicit opt-in only)
    if canary_opt_in and canary_target:
        decision["canary"]["status"] = "corroborating_run"
        try:
            live_result = runner._live_probe(canary_target)
            decision["canary"]["result"] = live_result.to_dict()["gate"]
        except Exception as exc:
            decision["canary"]["error"] = redact_operational(str(exc))
            decision["canary"]["result"] = {"passed": False, "gating": False}
    elif not canary_opt_in:
        decision["canary"]["status"] = "disabled_by_policy"

    # Overall decision
    if len(decision["blockers"]) == 0:
        decision["release_gate"] = "passed"
        decision["status"] = "ready_for_release"
    else:
        decision["release_gate"] = "failed"
        decision["status"] = "blocked"

    return decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic release gate evaluation")
    parser.add_argument("--fixtures", action="store_true", help="run deterministic fixture evaluation")
    parser.add_argument("--canary", action="store_true", help="opt in to live canary evaluation")
    parser.add_argument("--canary-target", default=None, help="target URL for live canary probe")
    parser.add_argument("--output", default=None, help="path to save release decision JSON")
    args = parser.parse_args(argv)

    decision = run_release_gate(
        fixtures_only=args.fixtures,
        canary_opt_in=args.canary,
        canary_target=args.canary_target,
    )

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(decision, indent=2))
    return 0 if decision["release_gate"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())