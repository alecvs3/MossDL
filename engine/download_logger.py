"""High-fidelity, verbose download logger and benchmark recorder.

Records second-by-second speeds, drops, stalls, latency, segment counts,
and computes statistical stability metrics (p50, p95, p99, peak, standard deviation)
to allow direct, 1-to-1 scientific comparisons against tools like JDownloader.
All logs are saved to the project's download_logs/ directory.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


_SECRET_PATTERNS = re.compile(
    r"(?:authorization|bearer|token|password|secret|credential|api[_-]?key|cookie|session|signature)[\s:=]+([^\s,;&]+)",
    re.IGNORECASE,
)


def _sanitize_log_filename(name: str) -> str:
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(". ")
    return cleaned[:60] or "download"


def _redact_secrets(text: str) -> str:
    if not text:
        return ""
    return _SECRET_PATTERNS.sub(r"[REDACTED]", text)


@dataclass
class SecondSample:
    """Snapshot of transfer state at a specific second."""
    timestamp: float
    elapsed_seconds: float
    bytes_transferred: int
    bytes_interval: int
    speed_bytes_per_sec: float
    speed_mb_per_sec: float
    average_speed_mb_per_sec: float
    percent_complete: float
    eta_seconds: float | None = None
    is_stall: bool = False
    is_drop: bool = False
    drop_pct: float = 0.0
    speed_mbps: float = 0.0
    status_flag: str = "NORMAL"
    phase: str = "transferring"


@dataclass
class StallEvent:
    """Record of a transfer stall or sudden speed plummet."""
    timestamp: float
    elapsed_seconds: float
    duration_seconds: float
    bytes_at_stall: int
    speed_before_stall_mb_s: float
    speed_after_recovery_mb_s: float
    reason: str = "stalled"


@dataclass
class TransferEvent:
    """State transition, range allocation, or error event."""
    timestamp: float
    elapsed_seconds: float
    event_type: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class DownloadSessionLogger:
    """Accumulates second-by-second transfer telemetry and writes verbose diagnostic logs."""

    def __init__(
        self,
        task_id: str,
        display_name: str,
        source_url: str,
        provider: str,
        destination_path: str,
        total_size: int | None = None,
        backend: str = "custom",
        streams_count: int = 1,
        logs_dir: str | Path | None = None,
    ) -> None:
        self.task_id = task_id
        self.display_name = display_name
        self.source_url = source_url
        self.provider = provider
        self.destination_path = destination_path
        self.total_size = total_size or 0
        self.backend = backend
        self.streams_count = streams_count

        if logs_dir is None:
            self.logs_dir = Path(__file__).resolve().parents[1] / "download_logs"
        else:
            self.logs_dir = Path(logs_dir)

        self._lock = threading.Lock()
        self.started_at: float = time.time()
        self.finished_at: float | None = None
        self.last_sample_time: float = self.started_at
        self.last_sample_bytes: int = 0
        self.current_bytes: int = 0
        self.status: str = "started"
        self.error: str | None = None
        self.integrity_state: str = "unverified"
        self.phase: str = "transferring"

        # Time-series telemetry
        self.samples: list[SecondSample] = []
        self.stalls: list[StallEvent] = []
        self.events: list[TransferEvent] = []

        # Tracking state
        self._last_second_tick: float = self.started_at
        self._stall_start_time: float | None = None
        self._stall_start_bytes: int = 0
        self._pre_stall_speed: float = 0.0
        self._peak_speed_seen: float = 0.0
        self._was_stalled_previously: bool = False

        self.log_event("SESSION_INIT", f"Started tracking download for {display_name}", {
            "task_id": task_id,
            "provider": provider,
            "backend": backend,
            "total_size": total_size,
            "streams": streams_count,
        })

    def log_event(self, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        now = time.time()
        elapsed = max(0.0, now - self.started_at)
        ctx = dict(details or {})
        ctx["task_id"] = self.task_id
        ctx["display_name"] = self.display_name
        ctx["provider"] = self.provider
        with self._lock:
            self.events.append(TransferEvent(
                timestamp=now,
                elapsed_seconds=round(elapsed, 3),
                event_type=event_type,
                message=message,
                details=ctx,
            ))
        try:
            from .telemetry import telemetry_bus
            level = "ERROR" if "ERROR" in event_type or "FAIL" in event_type else "WARN" if "STALL" in event_type or "DROP" in event_type else "INFO"
            telemetry_bus.record(
                level=level,
                subsystem="engine:download",
                message=f"[{event_type}] {self.display_name}: {message}",
                context=ctx,
                tier="engine",
            )
        except Exception:
            pass

    def set_total_size(self, total_size: int | None) -> None:
        """Backfill an indeterminate Content-Length before the final sample."""
        if total_size is None or int(total_size) <= 0:
            return
        discovered = int(total_size)
        with self._lock:
            if self.total_size == discovered:
                return
            self.total_size = discovered
        self.log_event("TOTAL_DISCOVERED", f"Transfer total discovered: {discovered} bytes", {
            "total_bytes": discovered,
        })

    def set_phase(self, phase: str) -> None:
        """Record the engine-owned phase so verification is not misreported as a socket stall."""
        normalized = str(phase or "transferring").strip().lower()
        if not normalized:
            normalized = "transferring"
        with self._lock:
            if self.phase == normalized:
                return
            self.phase = normalized
        self.log_event("PHASE_CHANGED", f"Transfer phase changed to {normalized}", {"phase": normalized})

    def record_progress(
        self,
        completed_bytes: int,
        current_speed_bps: float = 0.0,
        average_speed_bps: float = 0.0,
        eta_seconds: float | None = None,
        *,
        force: bool = False,
    ) -> None:
        """Called upon byte reception to snapshot second-by-second telemetry."""
        now = time.time()
        with self._lock:
            self.current_bytes = completed_bytes
            # Sample once per ~1.0 second window (or at least 0.8s)
            elapsed_since_tick = now - self._last_second_tick
            if force or elapsed_since_tick >= 0.85:
                delta_bytes = max(0, completed_bytes - self.last_sample_bytes)
                actual_speed_bps = delta_bytes / elapsed_since_tick if elapsed_since_tick > 0 else current_speed_bps
                instant_speed_mb = actual_speed_bps / (1024 * 1024)
                avg_speed_mb = (average_speed_bps or actual_speed_bps) / (1024 * 1024)

                pct = (completed_bytes / self.total_size * 100) if self.total_size > 0 else 0.0
                # A transfer can have received the final byte while it is
                # still being flushed and integrity-verified. Reserve 100%
                # for the forced terminal sample emitted after verification.
                if not force and pct >= 100.0:
                    pct = 99.0
                total_elapsed = now - self.started_at

                current_phase = self.phase

                # Verification happens after the socket closes. It can have a
                # zero-byte interval while hashing a multi-gigabyte file, but
                # that is not a transport stall and must not contaminate the
                # transfer report.
                is_stall = False
                is_drop = False
                drop_pct = 0.0

                if current_phase not in {"verifying", "postprocessing", "completed"} and len(self.samples) > 2:
                    recent_avg = sum(s.speed_mb_per_sec for s in self.samples[-3:]) / 3.0
                    if recent_avg > 1.0 and instant_speed_mb < recent_avg * 0.5:
                        is_drop = True
                        drop_pct = round(((recent_avg - instant_speed_mb) / recent_avg) * 100, 1)

                # Absolute stall detection (< 50 KB/s when total_size > 5MB)
                # The forced sample is emitted after the transfer has already
                # completed and verified.  A zero-byte delta there is not a
                # socket stall; it is simply the terminal snapshot.
                if (current_phase not in {"verifying", "postprocessing", "completed"}
                        and not force and delta_bytes < 50 * 1024
                        and self.total_size > 5 * 1024 * 1024):
                    is_stall = True
                    if self._stall_start_time is None:
                        self._stall_start_time = now
                        self._stall_start_bytes = completed_bytes
                        self._pre_stall_speed = avg_speed_mb
                else:
                    if self._stall_start_time is not None:
                        stall_duration = now - self._stall_start_time
                        if stall_duration >= 1.0:
                            stall_evt = StallEvent(
                                timestamp=self._stall_start_time,
                                elapsed_seconds=round(self._stall_start_time - self.started_at, 2),
                                duration_seconds=round(stall_duration, 2),
                                bytes_at_stall=self._stall_start_bytes,
                                speed_before_stall_mb_s=round(self._pre_stall_speed, 2),
                                speed_after_recovery_mb_s=round(instant_speed_mb, 2),
                                reason="throughput_drop",
                            )
                            self.stalls.append(stall_evt)
                            try:
                                from .telemetry import telemetry_bus
                                telemetry_bus.record(
                                    level="WARN",
                                    subsystem="engine:transfer",
                                    message=f"[STALL_RECOVERY] {self.display_name} recovered from {stall_duration:.1f}s stall (resumed at {instant_speed_mb:.2f} MB/s)",
                                    context={
                                        "task_id": self.task_id,
                                        "stall_duration_s": round(stall_duration, 2),
                                        "resumed_speed_mb_s": round(instant_speed_mb, 2),
                                    },
                                    tier="engine",
                                )
                            except Exception:
                                pass
                        self._stall_start_time = None

                speed_mbps = round(instant_speed_mb * 8.0, 1)

                # Diagnostic status flag
                if force:
                    status_flag = "COMPLETED"
                    is_stall = False
                    is_drop = False
                    drop_pct = 0.0
                    self._stall_start_time = None
                    self._was_stalled_previously = False
                elif current_phase in {"verifying", "postprocessing"}:
                    status_flag = current_phase.upper()
                elif is_stall:
                    status_flag = "STALL"
                    if not self._was_stalled_previously:
                        try:
                            from .telemetry import telemetry_bus
                            telemetry_bus.record(
                                level="WARN",
                                subsystem="engine:transfer",
                                message=f"[STALL_DETECTED] {self.display_name} speed plummeted to near-zero ({instant_speed_mb:.2f} MB/s)",
                                context={"task_id": self.task_id, "bytes": completed_bytes},
                                tier="engine",
                            )
                        except Exception:
                            pass
                    self._was_stalled_previously = True
                elif is_drop:
                    status_flag = f"DROP -{int(drop_pct)}%"
                elif not self.samples or total_elapsed < 2.0:
                    status_flag = "RAMP-UP"
                elif instant_speed_mb > self._peak_speed_seen and instant_speed_mb > 1.0:
                    status_flag = "PEAK"
                    self._peak_speed_seen = instant_speed_mb
                elif instant_speed_mb > avg_speed_mb * 1.25 and instant_speed_mb > 2.0:
                    status_flag = "BURST"
                elif self._was_stalled_previously and not is_stall:
                    status_flag = "RECOVERY"
                    self._was_stalled_previously = False
                else:
                    status_flag = "STEADY"

                sample = SecondSample(
                    timestamp=now,
                    elapsed_seconds=round(total_elapsed, 2),
                    bytes_transferred=completed_bytes,
                    bytes_interval=delta_bytes,
                    speed_bytes_per_sec=round(actual_speed_bps, 1),
                    speed_mb_per_sec=round(instant_speed_mb, 2),
                    average_speed_mb_per_sec=round(avg_speed_mb, 2),
                    percent_complete=round(pct, 2),
                    eta_seconds=round(eta_seconds, 1) if eta_seconds else None,
                    is_stall=is_stall,
                    is_drop=is_drop,
                    drop_pct=drop_pct,
                    speed_mbps=speed_mbps,
                    status_flag=status_flag,
                    phase=current_phase,
                )
                self.samples.append(sample)
                self._last_second_tick = now
                self.last_sample_bytes = completed_bytes

                # Live terminal telemetry
                try:
                    eta_str = f"{eta_seconds:.1f}s" if eta_seconds is not None else "--"
                    cur_mb = f"{completed_bytes / (1024 * 1024):6.2f} MB"
                    tot_mb = f"{self.total_size / (1024 * 1024):6.2f} MB" if self.total_size > 0 else "? MB"
                    print(
                        f"[DOWNLOAD SEC #{len(self.samples):03d}] T+{int(total_elapsed):03d}s | "
                        f"{instant_speed_mb:6.2f} MB/s ({speed_mbps:6.1f} Mbps) | "
                        f"{pct:5.1f}% ({cur_mb} / {tot_mb}) | "
                        f"Avg: {avg_speed_mb:6.2f} MB/s | ETA: {eta_str:<6} | [{status_flag}]",
                        file=sys.stderr,
                        flush=True,
                    )
                except Exception:
                    pass

    def finalize(self, status: str = "completed", error: str | None = None, integrity_state: str = "verified") -> dict[str, str]:
        """Finalize the session, calculate summary statistics, and persist JSON and LOG files."""
        now = time.time()
        with self._lock:
            self.finished_at = now
            self.status = status
            self.error = error
            self.integrity_state = integrity_state
            self.phase = "completed" if status == "completed" else status
            if self.total_size <= 0 and self.current_bytes > 0:
                self.total_size = self.current_bytes

            # Close any open stall
            if self._stall_start_time is not None:
                duration = now - self._stall_start_time
                if duration >= 1.0:
                    self.stalls.append(StallEvent(
                        timestamp=self._stall_start_time,
                        elapsed_seconds=round(self._stall_start_time - self.started_at, 2),
                        duration_seconds=round(duration, 2),
                        bytes_at_stall=self._stall_start_bytes,
                        speed_before_stall_mb_s=round(self._pre_stall_speed, 2),
                        speed_after_recovery_mb_s=0.0,
                        reason="ended_in_stall",
                    ))
                self._stall_start_time = None

            total_duration = max(0.001, now - self.started_at)
            total_bytes = self.current_bytes
            overall_avg_mb_s = (total_bytes / (1024 * 1024)) / total_duration

            speeds = [s.speed_mb_per_sec for s in self.samples if s.speed_mb_per_sec > 0]
            peak_speed = max(speeds, default=overall_avg_mb_s)
            min_speed = min(speeds, default=0.0)

            # Statistical analysis
            if speeds:
                sorted_speeds = sorted(speeds)
                n = len(sorted_speeds)
                p50 = sorted_speeds[int(n * 0.50)]
                p95 = sorted_speeds[min(n - 1, int(n * 0.95))]
                p99 = sorted_speeds[min(n - 1, int(n * 0.99))]
                mean_spd = sum(speeds) / n
                variance = sum((x - mean_spd) ** 2 for x in speeds) / n
                std_dev = math.sqrt(variance)
                # Coefficient of variation (CV) - key benchmark stability metric
                stability_index = round((std_dev / mean_spd), 3) if mean_spd > 0 else 0.0
            else:
                p50 = p95 = p99 = overall_avg_mb_s
                std_dev = 0.0
                stability_index = 0.0

            # Calculate Jitter (average absolute change between consecutive seconds)
            if len(self.samples) > 1:
                diffs = [abs(self.samples[i].speed_mb_per_sec - self.samples[i-1].speed_mb_per_sec) for i in range(1, len(self.samples))]
                speed_jitter = round(sum(diffs) / len(diffs), 2)
            else:
                speed_jitter = 0.0

            # Calculate Ramp-up duration (seconds to first reach 75% of peak)
            threshold_75 = peak_speed * 0.75
            ramp_up_time = 0.0
            for s in self.samples:
                if s.speed_mb_per_sec >= threshold_75:
                    ramp_up_time = s.elapsed_seconds
                    break

            # Calculate Steady-state plateau time (seconds at >= 50% peak)
            threshold_50 = peak_speed * 0.50
            steady_seconds = sum(1 for s in self.samples if s.speed_mb_per_sec >= threshold_50)
            speed_per_stream = round(overall_avg_mb_s / max(1, self.streams_count), 2)

            summary = {
                "task_id": self.task_id,
                "display_name": self.display_name,
                "provider": self.provider,
                "backend": self.backend,
                "streams_allocated": self.streams_count,
                "status": self.status,
                "phase": self.phase,
                "error": self.error,
                "integrity_state": self.integrity_state,
                "total_bytes": total_bytes,
                "total_size_bytes": self.total_size,
                "total_duration_seconds": round(total_duration, 2),
                "overall_average_speed_mb_s": round(overall_avg_mb_s, 2),
                "peak_speed_mb_s": round(peak_speed, 2),
                "min_speed_mb_s": round(min_speed, 2),
                "median_speed_mb_s": round(p50, 2),
                "p95_speed_mb_s": round(p95, 2),
                "p99_speed_mb_s": round(p99, 2),
                "speed_standard_deviation": round(std_dev, 2),
                "speed_jitter_mb_s": speed_jitter,
                "ramp_up_seconds": round(ramp_up_time, 2),
                "steady_state_seconds": steady_seconds,
                "speed_per_stream_mb_s": speed_per_stream,
                "stability_index_cv": stability_index,
                "stability_rating": (
                    "Rock Solid (CV <= 0.15)" if stability_index <= 0.15
                    else "Good Stability (CV <= 0.30)" if stability_index <= 0.30
                    else "Moderate Fluctuation (CV <= 0.50)" if stability_index <= 0.50
                    else "High Volatility / Spiky (CV > 0.50)"
                ),
                "stall_incidents_count": len(self.stalls),
                "total_stalled_seconds": round(sum(s.duration_seconds for s in self.stalls), 2),
                "total_samples_recorded": len(self.samples),
                "source_url_redacted": _redact_secrets(self.source_url),
                "destination": self.destination_path,
                "started_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)),
                "finished_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            }

            # Ensure logs directory exists
            self.logs_dir.mkdir(parents=True, exist_ok=True)

            date_str = time.strftime("%Y%m%d_%H%M%S", time.gmtime(self.started_at))
            clean_name = _sanitize_log_filename(self.display_name)
            base_filename = f"{date_str}_{self.provider}_{clean_name}_{self.task_id[:8]}"

            json_path = self.logs_dir / f"{base_filename}.json"
            log_path = self.logs_dir / f"{base_filename}.log"

            # 1. Write full high-fidelity JSON log
            full_data = {
                "schema_version": "1.0.0",
                "summary": summary,
                "stalls": [asdict(s) for s in self.stalls],
                "events": [asdict(e) for e in self.events],
                "second_by_second_telemetry": [asdict(s) for s in self.samples],
            }
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(full_data, f, indent=2)

            # 2. Write Human-Readable Report (.log / markdown format)
            report_text = self._generate_readable_report(summary)
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(report_text)

            try:
                from .telemetry import telemetry_bus
                final_level = "ERROR" if self.status in {"failed", "error"} else "INFO"
                telemetry_bus.record(
                    level=final_level,
                    subsystem="engine:lifecycle",
                    message=(
                        f"[DOWNLOAD_{self.status.upper()}] {self.display_name} in {total_duration:.1f}s | "
                        f"Avg: {overall_avg_mb_s:.2f} MB/s | Peak: {peak_speed:.2f} MB/s | "
                        f"Stalls: {len(self.stalls)} | Size: {total_bytes / (1024*1024):.2f} MB"
                    ),
                    context={
                        "task_id": self.task_id,
                        "status": self.status,
                        "error": self.error,
                        "avg_speed_mb_s": round(overall_avg_mb_s, 2),
                        "peak_speed_mb_s": round(peak_speed, 2),
                        "stalls_count": len(self.stalls),
                        "json_log": str(json_path),
                        "readable_log": str(log_path),
                    },
                    tier="engine",
                )
            except Exception:
                pass

            return {
                "json_log": str(json_path),
                "readable_log": str(log_path),
            }

    def _generate_readable_report(self, summary: dict[str, Any]) -> str:
        """Generate a clean ASCII-chart, diagnostic breakdown report."""
        lines: list[str] = []
        lines.append("=" * 96)
        lines.append(f" NEXLOAD VERBOSE DOWNLOAD BENCHMARK & DIAGNOSTIC REPORT")
        lines.append("=" * 96)
        lines.append(f"File Name:        {summary['display_name']}")
        lines.append(f"Task ID:          {summary['task_id']}")
        lines.append(f"Provider:         {summary['provider'].upper()}")
        lines.append(f"Engine Backend:   {summary['backend']}")
        lines.append(f"Streams / Ranges: {summary['streams_allocated']} (avg {summary.get('speed_per_stream_mb_s', 0.0):.2f} MB/s per stream)")
        lines.append(f"Total Size:       {summary['total_bytes'] / (1024 * 1024):.2f} MB ({summary['total_bytes']:,} bytes)")
        lines.append(f"Status:           {summary['status'].upper()} (Integrity: {summary['integrity_state']})")
        lines.append(f"Destination:      {summary['destination']}")
        lines.append(f"Start Time:       {summary['started_at_iso']}")
        lines.append(f"Finish Time:      {summary['finished_at_iso']}")
        lines.append("-" * 96)
        lines.append(" KEY PERFORMANCE & STABILITY METRICS (Direct vs JDownloader Comparison)")
        lines.append("-" * 96)
        lines.append(f"Overall Avg Speed:   {summary['overall_average_speed_mb_s']:>8.2f} MB/s")
        lines.append(f"Median Speed (p50):  {summary['median_speed_mb_s']:>8.2f} MB/s")
        lines.append(f"Peak Speed:          {summary['peak_speed_mb_s']:>8.2f} MB/s")
        lines.append(f"Min Active Speed:    {summary['min_speed_mb_s']:>8.2f} MB/s")
        lines.append(f"95th Percentile:     {summary['p95_speed_mb_s']:>8.2f} MB/s")
        lines.append(f"99th Percentile:     {summary['p99_speed_mb_s']:>8.2f} MB/s")
        lines.append(f"Standard Deviation:  {summary['speed_standard_deviation']:>8.2f} MB/s")
        lines.append(f"Speed Jitter:        {summary.get('speed_jitter_mb_s', 0.0):>8.2f} MB/s/sec (sec-to-sec variation)")
        lines.append(f"Ramp-Up to 75% Peak: {summary.get('ramp_up_seconds', 0.0):>8.2f}s")
        lines.append(f"Steady Plateau:      {summary.get('steady_state_seconds', 0):>8d}s (time spent >= 50% peak)")
        lines.append(f"Stability Index:     {summary['stability_index_cv']:>8.3f} ({summary['stability_rating']})")
        lines.append(f"Stall Incidents:     {summary['stall_incidents_count']} (Total Stalled Time: {summary['total_stalled_seconds']}s)")
        lines.append(f"Total Duration:      {summary['total_duration_seconds']:>8.2f}s")
        lines.append("-" * 96)

        # Second-by-Second Telemetry Table
        if self.samples:
            lines.append(" SECOND-BY-SECOND TELEMETRY LOG (Every Recorded Second)")
            lines.append("-" * 96)
            lines.append(
                f" {'Second':^7} | {'Speed (MB/s)':^13} | {'Bitrate':^13} | {'Interval':^11} | "
                f"{'Downloaded':^12} | {'Progress':^9} | {'Moving Avg':^12} | {'ETA':^6} | {'Status':<10}"
            )
            lines.append(
                f" {'-'*7}-+-{'-'*13}-+-{'-'*13}-+-{'-'*11}-+-{'-'*12}-+-{'-'*9}-+-{'-'*12}-+-{'-'*6}-+-{'-'*10}"
            )
            for s in self.samples:
                sec_lbl = f"T+{int(s.elapsed_seconds):03d}s"
                spd_lbl = f"{s.speed_mb_per_sec:6.2f} MB/s"
                bit_lbl = f"{s.speed_mbps:6.1f} Mbps"
                int_lbl = f"{s.bytes_interval / (1024 * 1024):6.2f} MB"
                cur_lbl = f"{s.bytes_transferred / (1024 * 1024):6.2f} MB"
                pct_lbl = f"{s.percent_complete:5.1f}%"
                avg_lbl = f"{s.average_speed_mb_per_sec:6.2f} MB/s"
                eta_lbl = f"{s.eta_seconds:4.1f}s" if s.eta_seconds is not None else "--"
                flag_lbl = f"[{s.status_flag}]"
                lines.append(
                    f" {sec_lbl:<7} | {spd_lbl:>13} | {bit_lbl:>13} | {int_lbl:>11} | "
                    f"{cur_lbl:>12} | {pct_lbl:>9} | {avg_lbl:>12} | {eta_lbl:>6} | {flag_lbl:<10}"
                )
            lines.append("-" * 96)

        # Speed Distribution Histogram
        if self.samples:
            lines.append(" SPEED DISTRIBUTION HISTOGRAM")
            lines.append("-" * 96)
            brackets = [
                ("  0.0 -   2.0 MB/s", 0.0, 2.0),
                ("  2.0 -  10.0 MB/s", 2.0, 10.0),
                (" 10.0 -  25.0 MB/s", 10.0, 25.0),
                (" 25.0 -  50.0 MB/s", 25.0, 50.0),
                (" 50.0 - 100.0 MB/s", 50.0, 100.0),
                ("100.0+       MB/s", 100.0, float("inf")),
            ]
            total_samp = len(self.samples)
            for name, low, high in brackets:
                matching = [s for s in self.samples if low <= s.speed_mb_per_sec < high]
                cnt = len(matching)
                pct_b = (cnt / total_samp * 100) if total_samp > 0 else 0.0
                bar_len = int(pct_b / 3)
                bar = "█" * bar_len
                lines.append(f" {name} : {cnt:3d}s ({pct_b:5.1f}%) | {bar}")
            lines.append("-" * 96)

        # ASCII Speed Curve
        if self.samples:
            lines.append(" SECOND-BY-SECOND SPEED TIMELINE (ASCII Graph)")
            lines.append("-" * 96)
            max_spd = max((s.speed_mb_per_sec for s in self.samples), default=1.0)
            chart_height = 8
            width = min(len(self.samples), 60)
            step = max(1, len(self.samples) // width)

            downsampled = [
                self.samples[i] for i in range(0, len(self.samples), step)
            ][:width]

            for h in range(chart_height, 0, -1):
                level = (h / chart_height) * max_spd
                row = f"{level:5.1f} MB/s | "
                for s in downsampled:
                    if s.speed_mb_per_sec >= level:
                        row += "█"
                    elif s.speed_mb_per_sec >= level * 0.7:
                        row += "▄"
                    elif s.is_stall:
                        row += "!"
                    else:
                        row += " "
                lines.append(row)
            lines.append("          └" + "─" * len(downsampled))
            lines.append(f"           0s{' ' * (len(downsampled) - 8)}{summary['total_duration_seconds']:.1f}s")
            lines.append("-" * 96)

        # Stalls Breakdown
        if self.stalls:
            lines.append(" STALL & SPEED DROP INCIDENTS")
            lines.append("-" * 96)
            for i, st in enumerate(self.stalls, 1):
                lines.append(
                    f"[{i}] At {st.elapsed_seconds:6.1f}s: Stalled for {st.duration_seconds:.2f}s | "
                    f"Speed plummeted from {st.speed_before_stall_mb_s:.2f} MB/s -> Recovered at {st.speed_after_recovery_mb_s:.2f} MB/s "
                    f"({st.reason})"
                )
            lines.append("-" * 96)

        # Direct JDownloader 2 Comparison Card
        lines.append(" DIRECT JDOWNLOADER 2 COMPARISON CARD (Paste JDownloader results here)")
        lines.append("-" * 96)
        lines.append(f" {'Metric':<30} | {'Nexload Engine':<26} | {'JDownloader 2'}")
        lines.append(f" {'-'*30}-+-{'-'*26}-+-{'-'*34}")
        lines.append(f" {'Peak Speed':<30} | {summary['peak_speed_mb_s']:>8.2f} MB/s               | [           ] MB/s")
        lines.append(f" {'Median Speed (p50)':<30} | {summary['median_speed_mb_s']:>8.2f} MB/s               | [           ] MB/s")
        lines.append(f" {'95th Percentile (p95)':<30} | {summary['p95_speed_mb_s']:>8.2f} MB/s               | [           ] MB/s")
        lines.append(f" {'Stability (CV)':<30} | {summary['stability_index_cv']:>8.3f} ({summary['stability_rating'][:12]})  | [           ]")
        lines.append(f" {'Speed Jitter (Variation)':<30} | {summary.get('speed_jitter_mb_s', 0.0):>8.2f} MB/s/sec           | [           ] MB/s/sec")
        lines.append(f" {'Ramp-Up to 75% Peak':<30} | {summary.get('ramp_up_seconds', 0.0):>8.2f}s                  | [           ]s")
        lines.append(f" {'Steady-State Duration':<30} | {summary.get('steady_state_seconds', 0):>8d}s                  | [           ]s")
        lines.append(f" {'Total Stalled Seconds':<30} | {summary['total_stalled_seconds']:>8.2f}s                  | [           ]s")
        lines.append(f" {'Total Wall Duration':<30} | {summary['total_duration_seconds']:>8.2f}s                  | [           ]s")
        lines.append("-" * 96)

        # Event History
        lines.append(" LIFECYCLE & PROTOCOL EVENTS")
        lines.append("-" * 96)
        for ev in self.events[:30]:
            lines.append(f"+{ev.elapsed_seconds:6.2f}s [{ev.event_type:<14}] {ev.message}")
        if len(self.events) > 30:
            lines.append(f"... and {len(self.events) - 30} additional events (see JSON log for full record).")

        lines.append("=" * 96)
        lines.append(f" End of Report. Full telemetry data stored in corresponding .json file.")
        lines.append("=" * 96 + "\n")
        return "\n".join(lines)


class DownloadLoggerRegistry:
    """Singleton registry tracking active download session loggers across threads."""

    def __init__(self) -> None:
        self._loggers: dict[str, DownloadSessionLogger] = {}
        self._lock = threading.Lock()

    def create(
        self,
        task_id: str,
        display_name: str,
        source_url: str,
        provider: str,
        destination_path: str,
        total_size: int | None = None,
        backend: str = "custom",
        streams_count: int = 1,
    ) -> DownloadSessionLogger:
        logger = DownloadSessionLogger(
            task_id=task_id,
            display_name=display_name,
            source_url=source_url,
            provider=provider,
            destination_path=destination_path,
            total_size=total_size,
            backend=backend,
            streams_count=streams_count,
        )
        with self._lock:
            self._loggers[task_id] = logger
        return logger

    def get(self, task_id: str) -> DownloadSessionLogger | None:
        with self._lock:
            return self._loggers.get(task_id)

    def finalize(self, task_id: str, status: str = "completed", error: str | None = None, integrity_state: str = "verified") -> dict[str, str] | None:
        with self._lock:
            logger = self._loggers.pop(task_id, None)
        if logger:
            return logger.finalize(status=status, error=error, integrity_state=integrity_state)
        return None


# Global registry instance
download_loggers = DownloadLoggerRegistry()
