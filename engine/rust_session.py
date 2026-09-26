"""One long-lived transfer-core process, shared by every transfer.

The core used to be spawned per file: a fifty-file package meant fifty process
launches and fifty cold connection pools, and pausing meant killing the process
mid-write. A session keeps one process alive, so transfers to the same host
reuse connections and a pause asks the transfer to stop instead.

Messages are line-delimited JSON on the process's stdin and stdout. A single
reader thread demultiplexes what comes back: progress notifications are routed
by transfer id, responses by request id, and each waiting transfer reads from
its own queue.
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import queue
import subprocess
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .telemetry import telemetry_bus

# Sent to a waiting transfer when the process dies with its request in flight.
_PROCESS_LOST = object()


def find_transfer_core() -> Path | None:
    configured = os.environ.get("TRANSFER_CORE_PATH")
    candidates = [Path(configured)] if configured else []
    name = "transfer-core.exe" if os.name == "nt" else "transfer-core"
    root = Path(__file__).resolve().parents[1]
    for base in (root, Path.cwd()):
        candidates.append(base / "src-tauri" / "target" / "release" / name)
        candidates.append(base / "src-tauri" / "target" / "debug" / name)
    return next((path for path in candidates if path and path.is_file()), None)


class CoreUnavailable(RuntimeError):
    """The transfer-core binary could not be found or started."""


class CoreSession:
    """Owns the core process and routes messages to and from it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue[Any]] = {}
        self._transfers: dict[str, queue.Queue[Any]] = {}
        self.generation = 0
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._reader: threading.Thread | None = None

    # ── process lifecycle ────────────────────────────────────────────────

    def available(self) -> bool:
        return find_transfer_core() is not None

    def _start_locked(self) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        binary = find_transfer_core()
        if binary is None:
            raise CoreUnavailable("Rust transfer-core binary is unavailable")
        process = subprocess.Popen(
            [str(binary)],
            text=True,
            encoding="utf-8",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._process = process
        # Which build runs matters when reading speeds: an unoptimised core once
        # capped MEGA/Transfer.it decryption at ~40 MB/s.
        debug_build = binary.parent.name.lower() == "debug"
        telemetry_bus.record(
            level="INFO", subsystem="engine:transport", tier="engine",
            message=f"[CORE_STARTED] {'debug' if debug_build else 'release'} transfer-core: {binary}",
            context={"path": str(binary), "debug_build": debug_build, "pid": process.pid},
        )
        # Anything the old process held (its tunnels) is gone with it.
        self.generation += 1
        self._reader = threading.Thread(target=self._read_stdout, args=(process,), daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, args=(process,), daemon=True).start()
        return process

    def close(self) -> None:
        """Close stdin so the core finishes in flight work, then make sure it exits."""
        with self._lock:
            process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for pipe in (process.stdout, process.stderr):
            try:
                if pipe and not pipe.closed:
                    pipe.close()
            except OSError:
                pass

    # ── reading ──────────────────────────────────────────────────────────

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        try:
            for line in iter(process.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self._route(message)
        except (OSError, ValueError):
            pass
        finally:
            self._abandon_pending(process)

    def _route(self, message: dict[str, Any]) -> None:
        if message.get("method") == "progress":
            params = message.get("params") or {}
            inbox = self._transfers.get(str(params.get("transfer_id") or ""))
            if inbox is not None:
                inbox.put(message)
            return
        request_id = message.get("id")
        if isinstance(request_id, int):
            inbox = self._pending.pop(request_id, None)
            if inbox is not None:
                inbox.put(message)

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        try:
            for line in iter(process.stderr.readline, ""):
                if line.strip():
                    self._stderr_tail.append(line.rstrip())
        except (OSError, ValueError):
            pass

    def _abandon_pending(self, process: subprocess.Popen[str]) -> None:
        """Wake every waiting transfer once the process can no longer answer."""
        for inbox in list(self._pending.values()):
            inbox.put(_PROCESS_LOST)
        self._pending.clear()
        for inbox in list(self._transfers.values()):
            inbox.put(_PROCESS_LOST)
        with self._lock:
            if self._process is process:
                self._process = None
        # A crashed core leaves its pipes behind; the next session opens new
        # ones, so these would otherwise accumulate for the engine's lifetime.
        for pipe in (process.stdin, process.stdout, process.stderr):
            try:
                if pipe and not pipe.closed:
                    pipe.close()
            except (OSError, ValueError):
                pass

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    # ── sending ──────────────────────────────────────────────────────────

    def _send_locked(self, process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
        if process.stdin is None or process.stdin.closed:
            raise CoreUnavailable("Rust transfer-core is not accepting requests")
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def start_download(self, params: dict[str, Any]) -> tuple[str, queue.Queue[Any]]:
        """Send a download request, returning its transfer id and inbox."""
        transfer_id = uuid.uuid4().hex
        params = {**params, "transfer_id": transfer_id}
        inbox: queue.Queue[Any] = queue.Queue()
        with self._lock:
            process = self._start_locked()
            request_id = next(self._ids)
            self._pending[request_id] = inbox
            self._transfers[transfer_id] = inbox
            try:
                self._send_locked(process, {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "download",
                    "params": params,
                })
            except Exception:
                self._pending.pop(request_id, None)
                self._transfers.pop(transfer_id, None)
                raise
        return transfer_id, inbox

    def request(self, method: str, params: dict[str, Any], timeout: float) -> tuple[Any, int]:
        """One request and its answer, with the process generation that gave it."""
        inbox: queue.Queue[Any] = queue.Queue()
        with self._lock:
            process = self._start_locked()
            generation = self.generation
            request_id = next(self._ids)
            self._pending[request_id] = inbox
            try:
                self._send_locked(process, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            except Exception:
                self._pending.pop(request_id, None)
                raise
        try:
            message = inbox.get(timeout=timeout)
        except queue.Empty:
            raise CoreUnavailable(f"Rust transfer-core did not answer {method} within {timeout:.0f}s") from None
        finally:
            self._pending.pop(request_id, None)
        if message is _PROCESS_LOST:
            raise CoreUnavailable("Rust transfer-core stopped before answering")
        if message.get("error"):
            raise RuntimeError(str((message["error"] or {}).get("message") or message["error"]))
        return message.get("result"), generation

    def stop(self, transfer_id: str, *, cancel: bool) -> None:
        """Ask a running transfer to stop. Never raises: it is best effort."""
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            try:
                self._send_locked(process, {
                    "jsonrpc": "2.0",
                    "id": next(self._ids),
                    "method": "cancel" if cancel else "pause",
                    "params": {"transfer_id": transfer_id},
                })
            except Exception:
                pass

    def finish(self, transfer_id: str) -> None:
        self._transfers.pop(transfer_id, None)


_SESSION: CoreSession | None = None
_SESSION_LOCK = threading.Lock()


def session() -> CoreSession:
    """The process-wide core session."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = CoreSession()
            atexit.register(_SESSION.close)
        return _SESSION


def wait_for_result(
    inbox: "queue.Queue[Any]",
    *,
    progress: Callable[[int], None] | None,
    on_total: Callable[[int], None] | None,
    should_pause: Callable[[], bool],
    should_cancel: Callable[[], bool],
    request_stop: Callable[[bool], None],
) -> dict[str, Any]:
    """Consume a transfer's messages until it reports an outcome.

    Pause and cancel are asked for once each; the core answers with its own
    outcome, so the transfer always ends by reporting rather than by being
    killed part way through a write.
    """
    asked_to_stop = False
    while True:
        try:
            message = inbox.get(timeout=0.1)
        except queue.Empty:
            if not asked_to_stop and (should_cancel() or should_pause()):
                request_stop(should_cancel())
                asked_to_stop = True
            continue
        if message is _PROCESS_LOST:
            raise CoreUnavailable("Rust transfer-core stopped before answering")
        if message.get("method") == "progress":
            params = message.get("params") or {}
            total = params.get("total")
            if total is not None and on_total is not None:
                on_total(int(total))
            recorded = params.get("bytes")
            if recorded is not None and progress is not None:
                progress(int(recorded))
            continue
        return message


__all__ = [
    "CoreSession",
    "CoreUnavailable",
    "find_transfer_core",
    "session",
    "wait_for_result",
]
