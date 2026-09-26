"""Backend-neutral, secret-safe transfer telemetry."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any


_SENSITIVE_KEY = re.compile(r"(?:authorization|cookie|password|token|secret|signed.?url|credential|signature|api.?key)", re.I)
_URL = re.compile(r"(?:https?|ftp)://[^\s\"']+", re.I)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): "[redacted]" if _SENSITIVE_KEY.search(str(key)) else _redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _URL.sub("[redacted-url]", value)[:1000]
    return value


@dataclass(slots=True)
class TransferMetrics:
    """Monotonic accumulator for one admitted transfer operation."""

    task_id: str | None = None
    selected_backend: str | None = None
    available: bool = True
    availability_reason: str | None = None
    started_monotonic: float = field(default_factory=time.monotonic)
    queued_monotonic: float | None = None
    queue_wait_seconds: float = 0.0
    read_seconds: float = 0.0
    write_seconds: float = 0.0
    retry_count: int = 0
    refresh_count: int = 0
    throttle_events: int = 0
    throttle_seconds: float = 0.0
    backpressure_events: int = 0
    cooldown_seconds: float = 0.0
    clamp_target: int | None = None
    bytes_read: int = 0
    bytes_written: int = 0
    connections: int = 0
    integrity_outcome: str = "unverified"
    _started: bool = False

    def mark_queued(self, now: float | None = None) -> None:
        self.queued_monotonic = time.monotonic() if now is None else float(now)

    def start(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else float(now)
        if not self._started:
            self.started_monotonic = current
            if self.queued_monotonic is not None:
                self.queue_wait_seconds = max(0.0, current - self.queued_monotonic)
            self._started = True

    def record_connection(self, count: int = 1) -> None:
        self.connections = max(0, self.connections + int(count))

    def record_read(self, byte_count: int, duration_seconds: float = 0.0) -> None:
        self.bytes_read += max(0, int(byte_count))
        self.read_seconds += max(0.0, float(duration_seconds))

    def record_write(self, byte_count: int, duration_seconds: float = 0.0) -> None:
        self.bytes_written += max(0, int(byte_count))
        self.write_seconds += max(0.0, float(duration_seconds))

    def record_retry(self, count: int = 1) -> None:
        self.retry_count += max(0, int(count))

    def record_refresh(self, count: int = 1) -> None:
        self.refresh_count += max(0, int(count))

    def record_throttle(self, duration_seconds: float = 0.0) -> None:
        self.throttle_events += 1
        self.throttle_seconds += max(0.0, float(duration_seconds))

    def record_backpressure(self, cooldown_seconds: float = 0.0,
                            clamp_target: int | None = None) -> None:
        """Record one circuit-breaker backpressure decision for the UI/telemetry."""
        self.backpressure_events += 1
        self.cooldown_seconds = max(self.cooldown_seconds, max(0.0, float(cooldown_seconds)))
        if clamp_target is not None:
            target = max(1, int(clamp_target))
            self.clamp_target = target if self.clamp_target is None else min(self.clamp_target, target)

    def set_integrity(self, outcome: str | None) -> None:
        self.integrity_outcome = str(outcome or "unverified")[:100]

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        current = time.monotonic() if now is None else float(now)
        start = self.started_monotonic
        return _redact({
            "schema_version": 1,
            "task_id": self.task_id,
            "selected_backend": self.selected_backend,
            "available": bool(self.available),
            "availability_reason": self.availability_reason,
            "duration_seconds": max(0.0, current - start),
            "queue_wait_seconds": self.queue_wait_seconds,
            "read_seconds": self.read_seconds,
            "write_seconds": self.write_seconds,
            "retry_count": self.retry_count,
            "refresh_count": self.refresh_count,
            "throttle_events": self.throttle_events,
            "throttle_seconds": self.throttle_seconds,
            "backpressure_events": self.backpressure_events,
            "cooldown_seconds": self.cooldown_seconds,
            "clamp_target": self.clamp_target,
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
            "connections": self.connections,
            "integrity_outcome": self.integrity_outcome,
            "throughput_bytes_per_second": (self.bytes_written / max(current - start, 0.000001)),
        })

    def finish(self, task_id: str | None = None, integrity_outcome: str | None = None) -> dict[str, Any]:
        """Finalize and return the event-safe snapshot.

        EngineService owns the durable event emission, while this small
        compatibility method keeps callers from reaching into the sink after
        they already hold the operation accumulator.
        """
        if task_id is not None and self.task_id is None:
            self.task_id = str(task_id)
        self.start()
        if integrity_outcome is not None:
            self.set_integrity(integrity_outcome)
        return self.snapshot()


class TransportMetricsSink:
    """Thread-safe in-memory sink whose snapshots are safe for events/logs."""

    def __init__(self) -> None:
        self._metrics: dict[str, TransferMetrics] = {}
        self._lock = threading.RLock()

    def begin(self, task_id: str, selected_backend: str | None = None, *,
              available: bool = True, reason: str | None = None) -> TransferMetrics:
        metrics = TransferMetrics(task_id, selected_backend, bool(available), _redact(reason) if reason else None)
        metrics.mark_queued()
        with self._lock:
            self._metrics[str(task_id)] = metrics
        return metrics

    start = begin

    def get(self, task_id: str) -> TransferMetrics | None:
        with self._lock:
            return self._metrics.get(str(task_id))

    def snapshot(self, task_id: str) -> dict[str, Any] | None:
        metrics = self.get(task_id)
        return metrics.snapshot() if metrics else None

    def finish(self, task_id: str, integrity_outcome: str | None = None) -> dict[str, Any] | None:
        metrics = self.get(task_id)
        if metrics is None:
            return None
        metrics.start()
        if integrity_outcome is not None:
            metrics.set_integrity(integrity_outcome)
        return metrics.snapshot()

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._metrics.pop(str(task_id), None)


__all__ = ["TransferMetrics", "TransportMetricsSink"]
