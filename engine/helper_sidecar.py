from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .provider_sdk import catalog_entry_digest


OUTCOME_STATUSES = {"completed", "cancelled", "timed_out", "quota_exceeded", "crashed",
                    "hash_mismatch", "unsupported", "output_limit", "malformed_progress"}
SECRET_WORDS = ("authorization", "cookie", "password", "token", "secret", "signature", "credential")
FORBIDDEN_ENGINE_KEYS = {"db", "database", "engine", "engine_state", "lifecycle", "task_state",
                         "admission", "resource_manager", "scheduler", "lease", "queue"}


class HelperAdmissionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HelperArtifact:
    helper_id: str
    version: str
    executable: str
    sha256: str
    operations: frozenset[str]
    hosts: frozenset[str]

    @classmethod
    def from_catalog(cls, entry: dict[str, Any]) -> "HelperArtifact":
        required = {"id", "version", "executable", "sha256", "operations", "hosts"}
        if not required.issubset(entry):
            raise HelperAdmissionError("helper catalog entry is incomplete")
        if entry.get("digest") and str(entry["digest"]).lower() != catalog_entry_digest(
                {key: value for key, value in entry.items() if key != "digest"}):
            raise HelperAdmissionError("helper catalog digest mismatch")
        return cls(str(entry["id"]), str(entry["version"]), str(entry["executable"]),
                   str(entry["sha256"]).lower(), frozenset(map(str, entry["operations"])),
                   frozenset(map(str, entry["hosts"])))


@dataclass(slots=True)
class HelperOutcome:
    status: str
    helper_id: str
    operation: str
    host: str
    result: dict[str, Any] = field(default_factory=dict)
    progress: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _redact({"status": self.status, "helper_id": self.helper_id, "operation": self.operation,
                        "host": self.host, "result": self.result, "progress": self.progress,
                        "error": self.error})


def _redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if any(w in str(k).lower() for w in SECRET_WORDS)
                         else _redact(v, secrets)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[redacted]")
        parsed = urlsplit(result)
        if parsed.scheme in {"http", "https"} and parsed.query:
            result = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        return result
    return value


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _has_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in FORBIDDEN_ENGINE_KEYS or _has_forbidden_key(child)
                   for key, child in value.items())
    return isinstance(value, list) and any(_has_forbidden_key(child) for child in value)


def _host_name(value: str) -> str:
    host = (urlsplit(value).hostname or value).lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


class _Permit:
    def __init__(self, owner: "HelperSupervisor") -> None:
        self.owner = owner
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.owner._active -= 1


class HelperSupervisor:
    """Runs one catalog-admitted helper operation through one bounded worker."""

    def __init__(self, catalog: dict[str, Any] | list[dict[str, Any]], *, secret_manager: Any = None,
                 resource_manager: Any = None, event_publisher: Any = None, max_active: int = 1,
                 max_output_bytes: int = 128 * 1024, max_progress_events: int = 256,
                 heartbeat_timeout: float = 5.0, worker_timeout: float = 30.0,
                 python_executable: str | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        entries = catalog.get("helpers", catalog) if isinstance(catalog, dict) else catalog
        self.catalog = {str(e["id"]): HelperArtifact.from_catalog(e) for e in entries}
        self.secret_manager = secret_manager
        self.resource_manager = resource_manager
        self.events = event_publisher
        self.max_active = max(0, int(max_active))
        self.max_output_bytes = max(1024, int(max_output_bytes))
        self.max_progress_events = max(1, int(max_progress_events))
        self.heartbeat_timeout = max(0.1, float(heartbeat_timeout))
        self.worker_timeout = max(0.1, float(worker_timeout))
        self.python_executable = python_executable or sys.executable
        self.clock = clock
        self._active = 0
        self._lock = threading.Lock()

    def _admit(self, helper_id: str, operation: str, host: str, version: str | None = None) -> HelperArtifact:
        artifact = self.catalog.get(helper_id)
        if artifact is None:
            raise HelperAdmissionError("unsupported helper")
        path = Path(artifact.executable).resolve()
        if not path.is_file() or _digest(path) != artifact.sha256 or (version is not None and version != artifact.version):
            raise HelperAdmissionError("helper hash mismatch")
        normalized_host = _host_name(host)
        if operation not in artifact.operations or not any(normalized_host == _host_name(allowed) or
                                                          normalized_host.endswith("." + _host_name(allowed))
                                                          for allowed in artifact.hosts):
            raise HelperAdmissionError("helper operation or host is not allowlisted")
        return artifact

    def _permit(self) -> _Permit | None:
        with self._lock:
            if self._active >= self.max_active:
                return None
            self._active += 1
            return _Permit(self)

    def _emit(self, task_id: str | None, name: str, payload: dict[str, Any], dedupe: str) -> None:
        if self.events is not None:
            self.events.emit(name, task_id, _redact(payload), dedupe)

    def run(self, helper_id: str, operation: str, host: str, params: dict[str, Any] | None = None,
            *, credential_ref: str | None = None, cancel_event: threading.Event | None = None,
            task_id: str | None = None, version: str | None = None) -> HelperOutcome:
        try:
            artifact = self._admit(helper_id, operation, host, version)
        except HelperAdmissionError as exc:
            status = "hash_mismatch" if "hash" in str(exc) else "unsupported"
            outcome = HelperOutcome(status, helper_id, operation, host, error=str(exc))
            self._emit(task_id, "HelperOutcome", outcome.to_dict(), f"helper-outcome:{task_id or helper_id}:admission")
            return outcome
        permit = self._permit()
        if permit is None:
            outcome = HelperOutcome("quota_exceeded", helper_id, operation, host, error="helper quota exhausted")
            self._emit(task_id, "HelperOutcome", outcome.to_dict(), f"helper-outcome:{task_id or helper_id}:quota")
            return outcome
        secret = None
        try:
            if credential_ref and self.secret_manager is not None:
                secret = self.secret_manager.resolve(credential_ref)
            if credential_ref and not secret:
                outcome = HelperOutcome("unsupported", helper_id, operation, host, error="credential reference unavailable")
                self._emit(task_id, "HelperOutcome", outcome.to_dict(), f"helper-outcome:{task_id or helper_id}:credential")
                return outcome
            if _has_forbidden_key(params or {}):
                outcome = HelperOutcome("unsupported", helper_id, operation, host, error="helper params cannot control engine")
                self._emit(task_id, "HelperOutcome", outcome.to_dict(), f"helper-outcome:{task_id or helper_id}:params")
                return outcome
            request = {"operation": operation, "params": params or {}}
            if secret:
                request["credential_value"] = secret
            project_root = str(Path(__file__).resolve().parents[1])
            process_env = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1",
                           "PYTHONPATH": project_root}
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process = subprocess.Popen(
                [self.python_executable, "-m", "engine.helper_worker", "--executable", artifact.executable],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=process_env,
                cwd=str(Path(artifact.executable).resolve().parent),
                start_new_session=os.name != "nt", creationflags=creationflags,
            )
            assert process.stdin and process.stdout and process.stderr
            process.stdin.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
            process.stdin.close()
            events: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
            for stream in (process.stdout, process.stderr):
                threading.Thread(target=self._read, args=(stream, events), daemon=True).start()
            started = self.clock()
            last_heartbeat = started
            output_bytes = 0
            progress: list[dict[str, Any]] = []
            result: dict[str, Any] = {}
            status = "crashed"
            error = None
            eof = 0
            while process.poll() is None or eof < 2:
                now = self.clock()
                if cancel_event is not None and cancel_event.is_set():
                    status = "cancelled"
                    self._terminate(process)
                    break
                if now - started > self.worker_timeout:
                    status = "timed_out"
                    self._terminate(process)
                    break
                if now - last_heartbeat > self.heartbeat_timeout:
                    status = "timed_out"
                    self._terminate(process)
                    break
                try:
                    kind, raw = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if kind == "eof":
                    eof += 1
                    continue
                assert raw is not None
                output_bytes += len(raw)
                if output_bytes > self.max_output_bytes:
                    status = "output_limit"
                    self._terminate(process)
                    break
                try:
                    message = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    status, error = "malformed_progress", "helper message was not an object"
                    self._terminate(process)
                    break
                kind_name = message.get("type")
                if kind_name in {"heartbeat", "progress"}:
                    last_heartbeat = now
                if kind_name == "progress":
                    if len(progress) >= self.max_progress_events:
                        status = "output_limit"
                        self._terminate(process)
                        break
                    if not isinstance(message.get("done"), (int, float)) or not isinstance(message.get("total"), (int, float)):
                        status, error = "malformed_progress", "helper progress was malformed"
                        self._terminate(process)
                        break
                    progress.append(_redact({"done": message["done"], "total": message["total"]}, (secret or "",)))
                    self._emit(task_id, "HelperProgress", {"helper_id": helper_id, "operation": operation,
                                                             "progress": progress[-1]}, f"helper:{task_id}:{len(progress)}")
                elif kind_name == "result":
                    result = _redact(message.get("value", {}), (secret or "",))
                    status = str(message.get("status", "completed"))
                    if status not in OUTCOME_STATUSES:
                        status = "unsupported"
                    last_heartbeat = now
                elif kind_name == "error":
                    status = str(message.get("status", "crashed"))
                    error = str(message.get("message", "helper failed"))
                    break
            if process.poll() is None:
                self._terminate(process)
            process.wait(timeout=2)
            for stream_name in ("stdin", "stdout", "stderr"):
                stream = getattr(process, stream_name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if status == "crashed" and process.returncode not in (0, None):
                error = error or f"helper exited with code {process.returncode}"
            outcome = HelperOutcome(status, helper_id, operation, host, result, progress, _redact(error, (secret or "",)))
            self._emit(task_id, "HelperOutcome", outcome.to_dict(), f"helper-outcome:{task_id or id(outcome)}")
            return outcome
        finally:
            permit.release()

    async def run_async(self, helper_id: str, operation: str, host: str,
                        params: dict[str, Any] | None = None, *, credential_ref: str | None = None,
                        cancel_event: threading.Event | None = None, task_id: str | None = None,
                        version: str | None = None) -> HelperOutcome:
        """Join one operation to the engine's active-task admission semaphore."""
        semaphore = getattr(self.resource_manager, "active_tasks", None)
        if semaphore is None:
            return await asyncio.to_thread(self.run, helper_id, operation, host, params,
                                           credential_ref=credential_ref, cancel_event=cancel_event,
                                           task_id=task_id, version=version)
        async with semaphore:
            return await asyncio.to_thread(self.run, helper_id, operation, host, params,
                                           credential_ref=credential_ref, cancel_event=cancel_event,
                                           task_id=task_id, version=version)

    @staticmethod
    def _read(stream, events) -> None:
        for line in iter(stream.readline, b""):
            events.put(("line", line))
        events.put(("eof", None))

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
