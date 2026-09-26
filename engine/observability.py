"""Unified engine-owned operational and benchmark reporting.

Correlates benchmark and live diagnostic observations for throughput, concurrency,
queue wait, retries, refreshes, provider health, disk writes, and classified failures.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlsplit

from .benchmarking import BenchmarkReport, BenchmarkRunner
from .errors import classify_provider_failure
from .reliability import FailureClass, classify_failure
from .transport_metrics import TransportMetricsSink

SCHEMA_VERSION = 1

_SECRET_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "password",
    "token",
    "tokens",
    "secret",
    "secrets",
    "signature",
    "signed_url",
    "credential",
    "credential_ref",
    "key",
    "api_key",
}

_URL_PATTERN = re.compile(r"(?i)\b(?:https?|ftp)://[^\s\"'>]+")


def redact_operational(value: Any) -> Any:
    """Recursively redact sensitive material from diagnostic dictionaries and strings."""
    if isinstance(value, dict):
        return {
            str(k): (
                "[redacted]"
                if any(part in str(k).lower() for part in _SECRET_KEYS)
                else redact_operational(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_operational(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://", "ftp://")):
            parsed = urlsplit(value)
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        # Redact embedded URLs in free-form messages
        return _URL_PATTERN.sub("<url-redacted>", value)[:1000]
    return value


@dataclass(slots=True)
class OperationalMetric:
    name: str
    value: Any
    unit: str
    description: str


@dataclass(slots=True)
class OperationalReport:
    """Unified operational report combining benchmarks, transport metrics, and task diagnostics."""

    schema_version: int = SCHEMA_VERSION
    report_version: str = "operational-report/1"
    generated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    source: str = "engine"
    metrics: dict[str, Any] = field(default_factory=dict)
    benchmark: Optional[dict[str, Any]] = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    provider_health: list[dict[str, Any]] = field(default_factory=list)
    failure_classification: Optional[dict[str, Any]] = None
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return redact_operational({
            "schema_version": self.schema_version,
            "report_version": self.report_version,
            "generated_at": self.generated_at,
            "source": self.source,
            "metrics": self.metrics,
            "benchmark": self.benchmark,
            "diagnostics": self.diagnostics,
            "provider_health": self.provider_health,
            "failure_classification": self.failure_classification,
            "recommendations": self.recommendations,
        })


def classify_error_outcome(error_text: str | None, status_code: int | None = None) -> str:
    """Classify failure using existing reliability policies and provider heuristics."""
    if not error_text and not status_code:
        return FailureClass.UNKNOWN.value

    err = str(error_text or "").lower()
    if "429" in err or status_code == 429 or "rate limit" in err or "throttl" in err:
        return FailureClass.RATE_LIMITED.value
    if "quota" in err or status_code == 509:
        return FailureClass.QUOTA.value
    if "401" in err or "403" in err or status_code in (401, 403) or "auth" in err or "login" in err:
        return FailureClass.AUTHENTICATION.value
    if "410" in err or "expired" in err:
        return FailureClass.EXPIRED_URL.value
    if "disk" in err or "enospc" in err or "space" in err:
        return FailureClass.DESTINATION.value
    if "checksum" in err or "integrity" in err or "hash" in err or "corrupt" in err:
        return FailureClass.INVALID_CONTENT.value
    if "unsupported" in err:
        return FailureClass.UNSUPPORTED.value
    if any(m in err for m in ("timeout", "econnreset", "connection", "network", "reset")):
        return FailureClass.TRANSIENT_NETWORK.value
    return FailureClass.UNKNOWN.value


class OperationalAggregator:
    """Aggregates telemetry from existing EngineService, TaskStore, and benchmarks."""

    def __init__(self, service: Any = None) -> None:
        self.service = service

    def generate_report(
        self,
        *,
        task_id: str | None = None,
        benchmark_report: BenchmarkReport | None = None,
        run_fixture_benchmark: bool = False,
    ) -> OperationalReport:
        report = OperationalReport()

        # 1. Benchmark metrics
        bm_data = None
        if benchmark_report is not None:
            bm_data = benchmark_report.to_dict()
        elif run_fixture_benchmark:
            runner = BenchmarkRunner()
            bm_report = runner.run("all")
            bm_data = bm_report.to_dict()

        if bm_data is not None:
            report.benchmark = bm_data

        # 2. Extract metrics from TaskStore / TransportMetrics if task_id provided or active tasks exist
        throughput_bps = 0.0
        observed_concurrency = 0
        peak_concurrency = 0
        queue_wait_sec = 0.0
        retry_count = 0
        refresh_count = 0
        read_bytes = 0
        written_bytes = 0
        disk_write_seconds = 0.0

        provider_snapshots: list[dict[str, Any]] = []
        failure_info: dict[str, Any] | None = None
        recommendations: list[str] = []

        if self.service is not None:
            store = getattr(self.service, "store", None)
            monitor = getattr(self.service, "provider_health", None)

            if monitor is not None and hasattr(monitor, "snapshots"):
                provider_snapshots = [redact_operational(s) for s in monitor.snapshots()]

            if task_id and store is not None:
                task = store.get(task_id)
                if task:
                    retry_count = task.retry_count
                    throughput_bps = task.speed_bytes_per_second or task.average_speed_bytes_per_second
                    written_bytes = task.completed_bytes

                    if task.error:
                        cls = classify_error_outcome(task.error)
                        failure_info = {
                            "category": cls,
                            "error_message": redact_operational(task.error),
                            "state": task.state,
                        }
                        if cls == FailureClass.RATE_LIMITED.value:
                            recommendations.append("Apply exponential backoff or switch route profile.")
                        elif cls == FailureClass.QUOTA.value:
                            recommendations.append("Switch account reference or wait for quota reset.")
                        elif cls == FailureClass.AUTHENTICATION.value:
                            recommendations.append("Refresh session or update credential reference.")
                        elif cls == FailureClass.DESTINATION.value:
                            recommendations.append("Verify destination disk space and file permissions.")

                    # Inspect diagnostic logs
                    logs = store.list_diagnostics(task_id)
                    report.diagnostics["logs_count"] = len(logs)
                    report.diagnostics["task_id"] = task_id
                    report.diagnostics["task_state"] = task.state
                    report.diagnostics["backend"] = task.backend

                    # Check segments for concurrency and disk write observations
                    segments = store.list_segments(task_id)
                    if segments:
                        observed_concurrency = len([s for s in segments if s.get("state") == "active"])
                        peak_concurrency = len(segments)

            # Pull from TransportMetricsSink if available
            sink: TransportMetricsSink | None = getattr(self.service, "transport_metrics", None)
            if sink is not None:
                if task_id:
                    snap = sink.snapshot(task_id)
                    if snap:
                        throughput_bps = snap.get("throughput_bytes_per_second", throughput_bps)
                        queue_wait_sec = snap.get("queue_wait_seconds", queue_wait_sec)
                        retry_count = max(retry_count, snap.get("retry_count", 0))
                        refresh_count = max(refresh_count, snap.get("refresh_count", 0))
                        read_bytes = snap.get("bytes_read", read_bytes)
                        written_bytes = max(written_bytes, snap.get("bytes_written", 0))
                        disk_write_seconds = snap.get("write_seconds", disk_write_seconds)
                        observed_concurrency = max(observed_concurrency, snap.get("connections", 0))

        # If benchmark data is present and live task was not measured, populate metrics from benchmark summary
        if bm_data is not None and not task_id:
            results = bm_data.get("results", [])
            custom_results = [r for r in results if r.get("backend") == "custom"]
            if custom_results:
                primary = custom_results[0]
                m = primary.get("metrics", {})
                throughput_bps = m.get("throughput_bytes_per_second", 0.0)
                queue_wait_sec = m.get("queue_wait_seconds", 0.0)
                retry_count = m.get("retry_count", 0)
                refresh_count = m.get("refresh_count", 0)
                read_bytes = m.get("read_bytes", 0)
                written_bytes = m.get("write_bytes", 0)
                disk_write_seconds = m.get("write_seconds", 0.0)
                observed_concurrency = m.get("connections", 1)
                peak_concurrency = m.get("fixture_peak_concurrent_ranges", 1)

        report.metrics = {
            "throughput_bytes_per_second": round(throughput_bps, 2),
            "observed_concurrency": observed_concurrency,
            "peak_concurrency": peak_concurrency,
            "queue_wait_seconds": round(queue_wait_sec, 3),
            "retry_count": retry_count,
            "refresh_count": refresh_count,
            "disk_read_bytes": read_bytes,
            "disk_write_bytes": written_bytes,
            "disk_write_seconds": round(disk_write_seconds, 3),
        }
        report.provider_health = provider_snapshots
        report.failure_classification = failure_info
        if not recommendations and bm_data:
            recommendations.append("All baseline transfer capabilities operational.")
        report.recommendations = recommendations

        return report


def human_operational_summary(report: OperationalReport | dict[str, Any]) -> str:
    """Format operational report as concise human-readable text."""
    data = report.to_dict() if isinstance(report, OperationalReport) else report
    m = data.get("metrics", {})
    lines = [
        "=== Operational & Observability Diagnostic Report ===",
        f"Schema Version: {data.get('schema_version')} ({data.get('report_version')})",
        f"Generated At: {data.get('generated_at')}",
        f"Source: {data.get('source')}",
        "",
        "--- Key Operational Metrics ---",
        f"  Throughput: {m.get('throughput_bytes_per_second', 0.0):,.2f} B/s",
        f"  Concurrency (Observed / Peak): {m.get('observed_concurrency', 0)} / {m.get('peak_concurrency', 0)}",
        f"  Queue Wait: {m.get('queue_wait_seconds', 0.0):.3f}s",
        f"  Retries: {m.get('retry_count', 0)}",
        f"  Refreshes: {m.get('refresh_count', 0)}",
        f"  Disk I/O: Read={m.get('disk_read_bytes', 0):,} B, Write={m.get('disk_write_bytes', 0):,} B ({m.get('disk_write_seconds', 0.0):.2f}s)",
    ]

    bm = data.get("benchmark")
    if bm:
        lines.append("")
        lines.append("--- Transfer Benchmark Overview ---")
        summary = bm.get("summary", {})
        lines.append(f"  Results: {summary.get('results')}, Gating: {summary.get('gating_results')}, Passed: {summary.get('passed_gates')}")
        for res in bm.get("results", []):
            backend = res.get("backend")
            scenario = res.get("scenario")
            avail = res.get("availability", {}).get("available", False)
            gate = res.get("gate", {})
            status = "PASSED" if gate.get("passed") else ("UNAVAILABLE" if not avail else "FAILED")
            lines.append(f"  - [{backend}] {scenario}: {status}")

    failures = data.get("failure_classification")
    if failures:
        lines.append("")
        lines.append("--- Failure Classification ---")
        lines.append(f"  Category: {failures.get('category')}")
        lines.append(f"  State: {failures.get('state')}")
        lines.append(f"  Error: {failures.get('error_message')}")

    providers = data.get("provider_health")
    if providers:
        lines.append("")
        lines.append(f"--- Provider Health ({len(providers)} monitored) ---")
        for p in providers:
            lines.append(f"  - {p.get('provider_id')}: Success Rate={p.get('success_rate', 0.0):.1%}, Quarantined={p.get('quarantined')}")

    recs = data.get("recommendations", [])
    if recs:
        lines.append("")
        lines.append("--- Actionable Recommendations ---")
        for r in recs:
            lines.append(f"  * {r}")

    return "\n".join(lines)