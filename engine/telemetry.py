"""Unified engine-wide structured telemetry bus and redaction core.

Provides an always-on in-memory ring buffer (5,000 entries) and rotating disk JSONL file
for high-fidelity diagnostic tracing across Engine, Shell, and UI tiers.
Ensures zero-leak recursive secret scrubbing on all logged context and URLs.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, List, Optional, Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Levels in ascending severity
LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL")
_LEVEL_WEIGHTS = {lvl: idx for idx, lvl in enumerate(LOG_LEVELS)}

_EXACT_SECRET_KEYS = {
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
    "api_key",
    "apikey",
    "bearer",
    "session",
    "session_id",
    "auth",
}

_SECRET_SUBSTRINGS = {
    "password",
    "token",
    "secret",
    "cookie",
    "api_key",
    "apikey",
    "credential",
    "session_id",
    "bearer",
    "authorization",
    "continuation",
}

_URL_PATTERN = re.compile(r"(?i)\b(?:https?|ftp)://[^\s\"'>]+")


def redact_telemetry_data(value: Any) -> Any:
    """Recursively redact sensitive keys and tokenized query parameters from data."""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            k_str = str(k)
            k_lower = k_str.lower()
            is_secret = (
                k_lower in _EXACT_SECRET_KEYS
                or any(sub in k_lower for sub in _SECRET_SUBSTRINGS)
            )
            if is_secret:
                sanitized[k_str] = "[REDACTED]"
            else:
                sanitized[k_str] = redact_telemetry_data(v)
        return sanitized


    if isinstance(value, (list, tuple, set)):
        return [redact_telemetry_data(item) for item in value]

    if isinstance(value, str):
        if value.startswith(("http://", "https://", "ftp://")):
            return _redact_url_query_tokens(value)
        # Redact URLs embedded in free-form text messages
        if "://" in value:
            return _URL_PATTERN.sub(lambda m: _redact_url_query_tokens(m.group(0)), value)
        return value

    return value


def _redact_url_query_tokens(url_str: str) -> str:
    """Scrub sensitive query parameters while keeping domain and clean path intact."""
    try:
        parsed = urlsplit(url_str)
        if not parsed.query:
            return url_str
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        sanitized_pairs = []
        for qk, qv in query_pairs:
            qk_lower = qk.lower()
            if qk_lower in _EXACT_SECRET_KEYS or any(s in qk_lower for s in _SECRET_SUBSTRINGS):
                sanitized_pairs.append((qk, "[REDACTED]"))
            else:
                sanitized_pairs.append((qk, qv))
        new_query = urlencode(sanitized_pairs)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))
    except Exception:
        return "<url-redacted>"



@dataclass(slots=True)
class LogEvent:
    """Structured telemetry record matching the cross-tier contract."""
    id: int
    timestamp: str
    epoch_ms: int
    level: str
    tier: str
    subsystem: str
    message: str
    context: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    duration_ms: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "epoch_ms": self.epoch_ms,
            "level": self.level,
            "tier": self.tier,
            "subsystem": self.subsystem,
            "message": self.message,
            "context": self.context,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


class EngineTelemetryBus:
    """Central thread-safe telemetry bus buffering events and rotating disk logs."""

    def __init__(
        self,
        capacity: int = 5000,
        log_file: Optional[Union[str, Path]] = None,
        max_bytes: int = 50 * 1024 * 1024,  # 50 MB
        backup_count: int = 3,
    ) -> None:
        self._capacity = capacity
        self._buffer: Deque[LogEvent] = collections.deque(maxlen=capacity)
        self._lock = threading.RLock()
        self._seq_id = 0
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._log_file: Optional[Path] = Path(log_file) if log_file else None
        self._listeners: List[Callable[[LogEvent], None]] = []

        if self._log_file:
            try:
                self._log_file.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

    def set_log_file(self, log_file: Union[str, Path]) -> None:
        """Update the persistent log file destination."""
        with self._lock:
            self._log_file = Path(log_file)
            try:
                self._log_file.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

    def add_listener(self, listener: Callable[[LogEvent], None]) -> None:
        """Register a callback invoked on each new log event."""
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[LogEvent], None]) -> None:
        """Unregister a listener callback."""
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def record(
        self,
        level: str,
        subsystem: str,
        message: str,
        context: Optional[dict[str, Any]] = None,
        error: Optional[dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        tier: str = "engine",
    ) -> int:
        """Record a structured event into the ring buffer and rotating disk file."""
        level_normalized = level.upper() if level else "INFO"
        if level_normalized not in _LEVEL_WEIGHTS:
            level_normalized = "INFO"

        now = time.time()
        now_ms = int(now * 1000)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

        sanitized_context = redact_telemetry_data(context) if context is not None else None
        sanitized_error = redact_telemetry_data(error) if error is not None else None
        sanitized_message = redact_telemetry_data(message) if message else ""

        with self._lock:
            self._seq_id += 1
            event_id = self._seq_id

            event = LogEvent(
                id=event_id,
                timestamp=now_iso,
                epoch_ms=now_ms,
                level=level_normalized,
                tier=tier,
                subsystem=subsystem or "engine:core",
                message=sanitized_message,
                context=sanitized_context,
                error=sanitized_error,
                duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
            )

            self._buffer.append(event)
            self._write_to_disk_locked(event)

            # Fire listeners safely
            listeners = list(self._listeners)

        for callback in listeners:
            try:
                callback(event)
            except Exception:
                pass

        return event_id

    def query(
        self,
        since_id: Optional[int] = None,
        min_level: Optional[str] = None,
        subsystem: Optional[str] = None,
        limit: int = 200,
    ) -> List[dict[str, Any]]:
        """Query buffered log events with filtering and pagination."""
        min_weight = _LEVEL_WEIGHTS.get(min_level.upper(), 0) if min_level else 0
        limit = max(1, min(limit, self._capacity))

        with self._lock:
            matched: List[dict[str, Any]] = []
            for event in self._buffer:
                if since_id is not None and event.id <= since_id:
                    continue
                if _LEVEL_WEIGHTS.get(event.level, 0) < min_weight:
                    continue
                if subsystem and not self._subsystem_matches(event.subsystem, subsystem):
                    continue

                matched.append(event.to_dict())
                if len(matched) >= limit:
                    break

            return matched

    def _subsystem_matches(self, event_subsystem: str, filter_pattern: str) -> bool:
        """Check if an event's subsystem matches the filter pattern (supports * wildcard)."""
        if filter_pattern == "*" or filter_pattern == event_subsystem:
            return True
        if filter_pattern.endswith("*"):
            prefix = filter_pattern[:-1]
            return event_subsystem.startswith(prefix)
        return False

    def clear(self) -> None:
        """Clear the in-memory ring buffer (does not delete disk files)."""
        with self._lock:
            self._buffer.clear()

    def export_snapshot(self, target_path: Optional[Union[str, Path]] = None) -> str:
        """Export all current buffered events to a target JSON file path or a default snapshot."""
        with self._lock:
            events = [e.to_dict() for e in self._buffer]

        if not target_path:
            target_dir = self._log_file.parent if self._log_file else Path.cwd()
            target_path = target_dir / f"telemetry_export_{int(time.time())}.json"
        else:
            target_path = Path(target_path)

        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump({"exported_at": time.time(), "total_events": len(events), "events": events}, f, indent=2)

        return str(target_path)

    def _write_to_disk_locked(self, event: LogEvent) -> None:
        """Append log event as JSONL to disk with size-based rotation."""
        if not self._log_file:
            return

        try:
            # Check rotation
            if self._log_file.exists() and self._log_file.stat().st_size >= self._max_bytes:
                self._rotate_files_locked()

            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_dict(), separators=(",", ":")) + "\n")
        except Exception:
            # Never let disk write failure crash the application
            pass

    def _rotate_files_locked(self) -> None:
        """Rotate existing disk files (e.g. engine.jsonl -> engine.jsonl.1)."""
        try:
            for i in range(self._backup_count - 1, 0, -1):
                sfn = self._log_file.with_name(f"{self._log_file.name}.{i}")
                dfn = self._log_file.with_name(f"{self._log_file.name}.{i + 1}")
                if sfn.exists():
                    sfn.rename(dfn)
            if self._log_file.exists():
                dfn = self._log_file.with_name(f"{self._log_file.name}.1")
                self._log_file.rename(dfn)
        except Exception:
            pass


# Global singleton telemetry bus
telemetry_bus = EngineTelemetryBus()


class TelemetryLoggingHandler(logging.Handler):
    """Standard library logging Handler that forwards log records to the telemetry bus."""

    def __init__(self, bus: EngineTelemetryBus = telemetry_bus) -> None:
        super().__init__()
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level_name = "INFO"
            if record.levelno >= logging.CRITICAL:
                level_name = "FATAL"
            elif record.levelno >= logging.ERROR:
                level_name = "ERROR"
            elif record.levelno >= logging.WARNING:
                level_name = "WARN"
            elif record.levelno >= logging.INFO:
                level_name = "INFO"
            elif record.levelno >= logging.DEBUG:
                level_name = "DEBUG"
            else:
                level_name = "TRACE"

            error_info = None
            if record.exc_info and record.exc_info[0]:
                exc_type, exc_val, exc_tb = record.exc_info
                import traceback
                error_info = {
                    "type": getattr(exc_type, "__name__", "Exception"),
                    "message": str(exc_val),
                    "stack": "".join(traceback.format_exception(exc_type, exc_val, exc_tb)),
                }

            context = {
                "logger": record.name,
                "module": record.module,
                "file": record.filename,
                "line": record.lineno,
                "func": record.funcName,
                "thread_name": record.threadName,
            }

            self.bus.record(
                level=level_name,
                subsystem=f"engine:{record.name}",
                message=record.getMessage(),
                context=context,
                error=error_info,
            )
        except Exception:
            self.handleError(record)


_hooks_installed = False


def install_telemetry_hooks(bus: EngineTelemetryBus = telemetry_bus) -> None:
    """Attach telemetry handler to the root logger and intercept global uncaught exceptions."""
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True

    # Attach handler to root logger
    root_logger = logging.getLogger()
    handler = TelemetryLoggingHandler(bus)
    root_logger.addHandler(handler)

    # Global uncaught exception hooks
    original_excepthook = sys.excepthook

    def _global_excepthook(exc_type, exc_val, exc_tb):
        import traceback
        bus.record(
            level="FATAL",
            subsystem="engine:uncaught",
            message=f"Uncaught exception: {exc_val}",
            error={
                "type": getattr(exc_type, "__name__", "Exception"),
                "message": str(exc_val),
                "stack": "".join(traceback.format_exception(exc_type, exc_val, exc_tb)),
            },
        )
        if original_excepthook:
            original_excepthook(exc_type, exc_val, exc_tb)

    sys.excepthook = _global_excepthook

    # Thread uncaught exception hook (Python 3.8+)
    if hasattr(threading, "excepthook"):
        original_thread_excepthook = threading.excepthook

        def _thread_excepthook(args):
            import traceback
            bus.record(
                level="FATAL",
                subsystem="engine:thread_uncaught",
                message=f"Uncaught thread exception in {args.thread.name}: {args.exc_value}",
                error={
                    "type": getattr(args.exc_type, "__name__", "Exception"),
                    "message": str(args.exc_value),
                    "stack": "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
                },
                context={"thread_name": args.thread.name},
            )
            if original_thread_excepthook:
                original_thread_excepthook(args)

        threading.excepthook = _thread_excepthook

    bus.record(
        level="INFO",
        subsystem="engine:telemetry",
        message="Telemetry diagnostic bus initialized and hooks attached",
        context={"pid": os.getpid(), "python_version": sys.version.split()[0]},
    )
