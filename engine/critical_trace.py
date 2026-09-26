"""Opt-in, source-timestamped critical-path tracing for engine operations."""
from __future__ import annotations

import contextvars
import json
import os
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .telemetry import redact_telemetry_data


@dataclass(frozen=True, slots=True)
class TraceIdentity:
    run_id: str | None = None
    package_id: str | None = None
    task_id: str | None = None
    part_number: int | None = None
    attempt_id: str | int | None = None
    span_id: str | None = None
    parent_span_id: str | None = None


_identity: contextvars.ContextVar[TraceIdentity] = contextvars.ContextVar(
    "critical_trace_identity", default=TraceIdentity()
)
_external_sink: Any = None


def current_identity() -> TraceIdentity:
    return _identity.get()


def identity_payload() -> dict[str, Any]:
    value = _identity.get()
    return {name: getattr(value, name) for name in TraceIdentity.__dataclass_fields__
            if getattr(value, name) is not None}


def set_external_sink(sink: Any) -> None:
    """Install a process-boundary sink, used by isolated provider workers."""
    global _external_sink
    _external_sink = sink


@contextmanager
def bind(**values: Any) -> Iterator[TraceIdentity]:
    """Add correlation fields to the current execution context."""
    current = _identity.get()
    allowed = TraceIdentity.__dataclass_fields__
    updated = replace(current, **{key: value for key, value in values.items()
                                  if key in allowed and value is not None})
    token = _identity.set(updated)
    try:
        yield updated
    finally:
        _identity.reset(token)


def _json_value(value: Any) -> Any:
    clean = redact_telemetry_data(value)
    try:
        json.dumps(clean)
        return clean
    except (TypeError, ValueError):
        return repr(clean)[:500]


class CriticalTraceSession:
    """Single-writer JSONL session; producers never perform file I/O."""

    def __init__(self, output_dir: str | Path, run_id: str | None = None,
                 attributes: Mapping[str, Any] | None = None, queue_size: int = 20_000) -> None:
        self.run_id = run_id or str(uuid4())
        self.started_wall_ns = time.time_ns()
        self.started_mono_ns = time.perf_counter_ns()
        self.directory = Path(output_dir).resolve() / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.trace_path = self.directory / "trace.jsonl"
        self.manifest_path = self.directory / "manifest.json"
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max(100, queue_size))
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._dropped = 0
        self._accepted = 0
        self._closed = False
        self._attributes = _json_value(dict(attributes or {}))
        self._thread = threading.Thread(target=self._write_loop,
                                        name=f"CriticalTrace-{self.run_id[:8]}", daemon=True)
        self._write_manifest("running")
        self._thread.start()

    @property
    def dropped(self) -> int:
        return self._dropped

    def record(self, event: str, phase: str, *, identity: TraceIdentity | None = None,
               resource: str | None = None, resource_id: str | None = None,
               attributes: Mapping[str, Any] | None = None) -> bool:
        if self._closed:
            return False
        stamped_ns = time.perf_counter_ns()
        ident = identity or _identity.get()
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        row = {
            "schema": 1,
            "sequence": sequence,
            "run_id": self.run_id,
            "t_mono_ns": stamped_ns,
            "offset_ns": stamped_ns - self.started_mono_ns,
            "thread_id": threading.get_ident(),
            "process_id": os.getpid(),
            "event": event,
            "phase": phase,
            "package_id": ident.package_id,
            "task_id": ident.task_id,
            "part_number": ident.part_number,
            "attempt_id": ident.attempt_id,
            "span_id": ident.span_id,
            "parent_span_id": ident.parent_span_id,
            "resource": resource,
            "resource_id": resource_id,
            "attributes": _json_value(dict(attributes or {})),
        }
        try:
            self._queue.put_nowait(row)
            self._accepted += 1
            return True
        except queue.Full:
            self._dropped += 1
            return False

    def ingest_external(self, row: Mapping[str, Any]) -> bool:
        """Accept a source-stamped record from an isolated local process."""
        if self._closed:
            return False
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        source_ns = int(row.get("t_mono_ns") or time.perf_counter_ns())
        clean = {
            "schema": 1,
            "sequence": sequence,
            "run_id": self.run_id,
            "t_mono_ns": source_ns,
            "offset_ns": source_ns - self.started_mono_ns,
            "thread_id": row.get("thread_id"),
            "process_id": row.get("process_id"),
            "event": str(row.get("event") or "observation"),
            "phase": str(row.get("phase") or "plugin.unknown"),
            "package_id": row.get("package_id"),
            "task_id": row.get("task_id"),
            "part_number": row.get("part_number"),
            "attempt_id": row.get("attempt_id"),
            "span_id": row.get("span_id"),
            "parent_span_id": row.get("parent_span_id"),
            "resource": row.get("resource"),
            "resource_id": row.get("resource_id"),
            "attributes": _json_value(row.get("attributes") or {}),
        }
        try:
            self._queue.put_nowait(clean)
            self._accepted += 1
            return True
        except queue.Full:
            self._dropped += 1
            return False

    def close(self) -> dict[str, Any]:
        if self._closed:
            return self.summary()
        self.record("state", "trace.stop", attributes={"dropped_events": self._dropped})
        self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=10.0)
        self._write_manifest("complete" if not self._thread.is_alive() else "writer_timeout")
        return self.summary()

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "directory": str(self.directory),
            "trace_path": str(self.trace_path),
            "accepted_events": self._accepted,
            "dropped_events": self._dropped,
            "valid": self._dropped == 0 and not self._thread.is_alive(),
        }

    def _write_loop(self) -> None:
        with self.trace_path.open("w", encoding="utf-8", newline="\n") as handle:
            while True:
                row = self._queue.get()
                if row is None:
                    break
                handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n")
            handle.flush()

    def _write_manifest(self, status: str) -> None:
        manifest = {
            "schema": 1,
            "run_id": self.run_id,
            "status": status,
            "started_wall_ns": self.started_wall_ns,
            "started_mono_ns": self.started_mono_ns,
            "finished_wall_ns": time.time_ns() if status != "running" else None,
            "accepted_events": self._accepted,
            "dropped_events": self._dropped,
            "attributes": self._attributes,
        }
        self.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


_session_lock = threading.RLock()
_session: CriticalTraceSession | None = None


def start(output_dir: str | Path, *, run_id: str | None = None,
          attributes: Mapping[str, Any] | None = None, queue_size: int = 20_000) -> dict[str, Any]:
    global _session
    with _session_lock:
        if _session is not None:
            raise RuntimeError("a critical trace session is already active")
        _session = CriticalTraceSession(output_dir, run_id, attributes, queue_size)
        _session.record("state", "trace.start", attributes={"wall_anchor_ns": _session.started_wall_ns})
        return _session.summary()


def stop() -> dict[str, Any]:
    global _session
    with _session_lock:
        if _session is None:
            return {"active": False}
        session = _session
        _session = None
    return session.close()


def active() -> bool:
    return _session is not None


def ingest_external(row: Mapping[str, Any]) -> bool:
    session = _session
    return bool(session and session.ingest_external(row))


def mark(phase: str, *, event: str = "observation", resource: str | None = None,
         resource_id: str | None = None, identity: TraceIdentity | None = None,
         **attributes: Any) -> bool:
    session = _session
    if session:
        return session.record(event, phase, identity=identity, resource=resource,
                              resource_id=resource_id, attributes=attributes)
    sink = _external_sink
    if sink:
        ident = identity or _identity.get()
        row = {
            "schema": 1, "t_mono_ns": time.perf_counter_ns(),
            "thread_id": threading.get_ident(), "process_id": os.getpid(),
            "event": event, "phase": phase,
            "package_id": ident.package_id, "task_id": ident.task_id,
            "part_number": ident.part_number, "attempt_id": ident.attempt_id,
            "span_id": ident.span_id, "parent_span_id": ident.parent_span_id,
            "resource": resource, "resource_id": resource_id,
            "attributes": _json_value(attributes),
        }
        try:
            sink(row)
            return True
        except Exception:
            return False
    return False


@contextmanager
def span(phase: str, *, resource: str | None = None, resource_id: str | None = None,
         **attributes: Any) -> Iterator[str]:
    parent = _identity.get()
    span_id = str(uuid4())
    child = replace(parent, span_id=span_id, parent_span_id=parent.span_id)
    token = _identity.set(child)
    mark(phase, event="span_start", resource=resource, resource_id=resource_id, **attributes)
    status = "ok"
    try:
        yield span_id
    except BaseException as exc:
        status = "error"
        mark(phase, event="state", error_type=type(exc).__name__)
        raise
    finally:
        mark(phase, event="span_end", resource=resource, resource_id=resource_id, status=status)
        _identity.reset(token)
