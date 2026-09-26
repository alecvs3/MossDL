"""Log Analyzer for Transfer Manager structured telemetry.

Audits engine.jsonl and download_logs/ for lifecycle transitions,
CAPTCHA solver events, provider errors, and anomaly patterns.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import get_log_paths


class LogAuditReport:
    """Summary container for audited log events."""

    def __init__(self) -> None:
        self.total_records = 0
        self.task_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.captcha_events: list[dict[str, Any]] = []
        self.solver_events: list[dict[str, Any]] = []
        self.provider_errors: list[dict[str, Any]] = []
        self.rate_limit_events: list[dict[str, Any]] = []
        self.intercepted_links: list[dict[str, Any]] = []
        self.exceptions: list[dict[str, Any]] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_records": self.total_records,
            "tasks_observed": len(self.task_events),
            "captcha_detections": len(self.captcha_events),
            "solver_events": len(self.solver_events),
            "provider_errors": len(self.provider_errors),
            "rate_limits": len(self.rate_limit_events),
            "intercepted_links": len(self.intercepted_links),
            "exceptions": len(self.exceptions),
        }


def read_engine_logs(limit: int = 1000, path: Path | None = None) -> list[dict[str, Any]]:
    """Read the latest records from engine.jsonl."""
    log_file = path or get_log_paths().engine_jsonl
    if not log_file.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    records.append(json.loads(line_str))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []

    return records[-limit:] if limit > 0 else records


def audit_logs(records: list[dict[str, Any]]) -> LogAuditReport:
    """Audit parsed log records and categorize lifecycle events."""
    report = LogAuditReport()
    report.total_records = len(records)

    for rec in records:
        msg = rec.get("message") or rec.get("msg") or ""
        msg_lower = msg.lower()
        subsystem = rec.get("subsystem") or rec.get("logger") or ""
        ctx = rec.get("context") or {}
        task_id = ctx.get("task_id") or rec.get("task_id")

        if task_id:
            report.task_events[task_id].append(rec)

        # CAPTCHA & Solver audit
        if "[captcha_detected]" in msg_lower or "[captcha_challenge]" in msg_lower:
            report.captcha_events.append(rec)
        if "[captcha_bypassed]" in msg_lower or "[challenge_solved]" in msg_lower:
            report.solver_events.append(rec)
        if "[solver_intercept]" in msg_lower or "[solver_dom_link]" in msg_lower:
            report.intercepted_links.append(rec)
        if "[solver_navigation_failed]" in msg_lower or "[challenge_timeout]" in msg_lower:
            report.exceptions.append(rec)

        # Provider errors & rate limits
        if "[provider_rejected]" in msg_lower or "quota_locked" in msg_lower:
            report.provider_errors.append(rec)
        if "rate_limit" in msg_lower or "download limit" in msg_lower:
            report.rate_limit_events.append(rec)

        # General error/exception levels
        level = (rec.get("level") or "").upper()
        if level in ("ERROR", "CRITICAL") and rec not in report.exceptions:
            report.exceptions.append(rec)

    return report


def format_report_summary(report: LogAuditReport) -> str:
    """Format audit results into readable Markdown text."""
    lines = [
        "## Engine Structured Telemetry Audit Report",
        f"- **Total Records Inspected**: {report.total_records}",
        f"- **Unique Tasks Observed**: {len(report.task_events)}",
        f"- **CAPTCHA Challenges Detected**: {len(report.captcha_events)}",
        f"- **Bypasses / Solves Recorded**: {len(report.solver_events)}",
        f"- **Direct URLs Intercepted by Solver**: {len(report.intercepted_links)}",
        f"- **Provider Rejections / Quota Events**: {len(report.provider_errors)}",
        f"- **Rate Limit Warnings**: {len(report.rate_limit_events)}",
        f"- **Errors / Exceptions Logged**: {len(report.exceptions)}",
    ]

    if report.exceptions:
        lines.append("\n### Recent Errors / Exceptions:")
        for exc in report.exceptions[-5:]:
            msg = exc.get("message") or exc.get("msg") or str(exc)
            lines.append(f"- `[{exc.get('subsystem', 'engine')}]` {msg[:120]}")

    if report.solver_events:
        lines.append("\n### Recent Successful Solver Bypasses:")
        for sol in report.solver_events[-5:]:
            msg = sol.get("message") or sol.get("msg") or str(sol)
            lines.append(f"- `[{sol.get('subsystem', 'engine')}]` {msg[:120]}")

    return "\n".join(lines)
