from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .errors import DownloadCanceled, NeedsUser

logger = logging.getLogger(__name__)

ARCHIVE_STAGING_PREFIX = ".transfer-archive-staging-"
ARCHIVE_COMPLETE_PREFIX = ".transfer-archive-complete-"


def _sanitize_job_id(job_id: str) -> str:
    return "".join(char if char.isascii() and char.isalnum() else "_" for char in str(job_id))


def completion_sentinel_path(output_directory: str | Path, job_id: str) -> Path:
    """Sentinel written by archive-worker only after a fully successful promote."""
    return Path(output_directory) / f"{ARCHIVE_COMPLETE_PREFIX}{_sanitize_job_id(job_id)}"


def find_archive_worker() -> Path | None:
    configured = os.environ.get("ARCHIVE_WORKER_PATH")
    candidates = [Path(configured)] if configured else []
    worker_name = "archive-worker.exe" if os.name == "nt" else "archive-worker"
    candidates.extend(
        [
            Path.cwd() / "src-tauri" / "target" / "release" / worker_name,
            Path.cwd() / "src-tauri" / "target" / "debug" / worker_name,
            Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "release" / worker_name,
            Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "debug" / worker_name,
        ]
    )
    return next((path for path in candidates if path and path.is_file()), None)


class ArchiveBackend:
    """Supervises the isolated Rust archive worker over JSON-RPC."""

    def __init__(self) -> None:
        self.call_timeout: float = float(os.environ.get("ARCHIVE_WORKER_TIMEOUT", "3600"))
        self.cancel_grace: float = float(os.environ.get("ARCHIVE_WORKER_CANCEL_GRACE", "5"))
        self.release_timeout: float = float(os.environ.get("ARCHIVE_WORKER_RELEASE_TIMEOUT", "5"))

    @staticmethod
    def _path_locked(path: Path) -> bool:
        """True while an existing file cannot yet be renamed/unlinked by this process.

        Rename-to-self exercises exactly the share semantics unlink needs on
        Windows, without mutating anything on success.
        """
        if not path.is_file():
            return False
        try:
            os.replace(path, path)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return False

    @staticmethod
    def wait_for_path_release(paths: Iterable[str | Path], timeout: float = 30.0,
                              poll_interval: float = 0.05) -> bool:
        """Bounded poll until every existing path can be exclusively reopened.

        Returns True once all paths are releasable, False on timeout. Callers
        must treat a False result as "do not delete yet".
        """
        candidates = [Path(path) for path in paths if path]
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            blocked = [path for path in candidates if ArchiveBackend._path_locked(path)]
            if not blocked:
                return True
            if time.monotonic() >= deadline:
                logger.warning(
                    "archive-worker handles still held after %.1fs: %s",
                    timeout, ", ".join(str(path) for path in blocked))
                return False
            time.sleep(max(0.005, min(poll_interval, 0.25)))

    @staticmethod
    def _close_worker_stdin(process: subprocess.Popen[str]) -> None:
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except OSError:
            pass

    def _cancel_worker(self, process: subprocess.Popen[str], params: dict[str, Any], method: str) -> bool:
        """Prefer the worker's own unwind (cancel RPC + stdin EOF) over tree-kill."""
        job_id = str(params.get("job_id") or "")
        delivered = False
        try:
            if process.stdin and not process.stdin.closed:
                cancel = {"jsonrpc": "2.0", "id": 2, "method": "cancel", "params": {"job_id": job_id}}
                process.stdin.write(json.dumps(cancel) + "\n")
                process.stdin.flush()
                delivered = True
        except OSError as exc:
            logger.warning("archive-worker cancel could not be delivered for %s: %s", method, exc)
        self._close_worker_stdin(process)
        try:
            process.wait(timeout=self.cancel_grace)
        except subprocess.TimeoutExpired:
            logger.warning(
                "archive-worker ignored %s cancel (job_id=%s); escalating to tree-kill",
                method, job_id or "<none>")
            self._terminate_process_tree(process)
            return False
        logger.info(
            "archive-worker %s canceled via worker cleanup (job_id=%s, delivered=%s)",
            method, job_id or "<none>", delivered)
        return True

    def _ensure_handles_released(self, params: dict[str, Any], method: str) -> None:
        """After the worker exits, confirm its input/output paths are unlocked."""
        existing = [path for path in (params.get("path"), params.get("output_directory"))
                    if path and Path(str(path)).exists()]
        if existing and not self.wait_for_path_release(existing, timeout=self.release_timeout):
            logger.warning("archive-worker %s exited with paths still locked", method)

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        """Terminate the worker and descendants, including Windows child trees."""
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def available(self) -> bool:
        return find_archive_worker() is not None

    def call(self, method: str, params: dict[str, Any], control=None) -> dict[str, Any]:
        worker = find_archive_worker()
        if worker is None:
            raise RuntimeError("archive-worker binary is unavailable")
        process_options: dict[str, Any] = {
            "text": True, "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        }
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        process = subprocess.Popen([str(worker)], **process_options)
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        response_ready = threading.Event()

        def _drain(stream: Any, sink: list[str], signal: threading.Event | None = None) -> None:
            try:
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    sink.append(line)
                    if signal is not None:
                        signal.set()
            except Exception as err:
                logger.warning("archive worker pipe drain failed: %s", err)

        try:
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            # Drain both pipes concurrently so a verbose worker can never block on
            # a full OS pipe buffer while the supervisor waits for the response.
            out_reader = threading.Thread(target=_drain, args=(process.stdout, stdout_chunks, response_ready), daemon=True)
            err_reader = threading.Thread(target=_drain, args=(process.stderr, stderr_chunks), daemon=True)
            out_reader.start()
            err_reader.start()
            deadline = time.time() + self.call_timeout
            while not response_ready.is_set() and process.poll() is None:
                if control and control.cancel.is_set():
                    # The worker owns staging cleanup and file handles; give it a
                    # chance to unwind before the supervisor forces termination.
                    self._cancel_worker(process, params, method)
                    raise DownloadCanceled()
                if time.time() > deadline:
                    self._terminate_process_tree(process)
                    raise TimeoutError(f"archive-worker did not respond within {self.call_timeout:.0f}s")
                response_ready.wait(0.05)
            # stdin stays open while the job runs so cancel can be delivered; EOF
            # is what lets the worker leave its read loop and exit.
            self._close_worker_stdin(process)
            if process.poll() is None:
                try:
                    process.wait(timeout=min(self.cancel_grace, 30.0))
                except subprocess.TimeoutExpired:
                    self._terminate_process_tree(process)
                    raise RuntimeError("archive-worker did not exit after returning a response")
            out_reader.join(timeout=5)
            err_reader.join(timeout=5)
            for stream in (process.stdout, process.stderr):
                try:
                    if stream and not stream.closed:
                        stream.close()
                except OSError:
                    pass
        except DownloadCanceled:
            raise
        except Exception:
            self._terminate_process_tree(process)
            raise
        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if process.returncode != 0:
            raise RuntimeError(stderr.strip() or "archive-worker exited unsuccessfully")
        lines = [line for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("archive-worker returned no response")
        response = json.loads(lines[-1])
        if response.get("error"):
            message = str(response["error"].get("message", "archive operation failed"))
            if stderr.strip():
                logger.warning("archive-worker %s reported an error: %s", method, stderr.strip())
            lowered = message.casefold()
            if any(marker in lowered for marker in ("password", "encrypted archive", "encrypted zip", "requires a supervisor-resolved secret")):
                raise NeedsUser("archive password is required", action="archive_password")
            raise RuntimeError(message)
        # The worker has exited, so its handles are gone; confirm before the
        # caller is allowed to delete the source volumes.
        self._ensure_handles_released(params, method)
        return response.get("result") or {}

    def _params(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return {"path": str(Path(path).resolve()), **kwargs}

    @staticmethod
    def _archive_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
        """Translate the scheduler policy vocabulary to the worker's strict schema."""
        policy = policy or {}
        return {
            "max_output_bytes": int(policy.get("archive_max_output_bytes", policy.get("max_output_bytes", 500 * 1024**3))),
            "max_file_count": int(policy.get("archive_max_file_count", policy.get("max_file_count", 100_000))),
            "max_nesting_depth": int(policy.get("archive_max_nesting_depth", policy.get("max_nesting_depth", 32))),
            "max_compression_ratio": int(policy.get("archive_max_compression_ratio", policy.get("max_compression_ratio", 1000))),
            "minimum_free_space": int(policy.get("min_free_space", policy.get("minimum_free_space", 1024**3))),
        }

    def probe(self, path: str | Path, password_ref: str | None = None, policy: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.call("probe", self._params(path, password_ref=password_ref, policy=self._archive_policy(policy)))

    def list(self, path: str | Path, password_ref: str | None = None, policy: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.call("list", self._params(path, password_ref=password_ref, policy=self._archive_policy(policy)))

    def test(self, path: str | Path, password_ref: str | None = None, policy: dict[str, Any] | None = None, control=None) -> dict[str, Any]:
        return self.call("test", self._params(path, password_ref=password_ref, job_id=f"test-{os.getpid()}", policy=self._archive_policy(policy)), control)

    def extract(
        self,
        path: str | Path,
        output_directory: str | Path,
        password_ref: str | None = None,
        policy: dict[str, Any] | None = None,
        control=None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        # job_id is append-only: durable jobs pass their store id so the worker's
        # completion sentinel is addressable across restarts.
        return self.call(
            "extract",
            self._params(
                path,
                output_directory=str(Path(output_directory).resolve()),
                password_ref=password_ref,
                job_id=job_id or f"extract-{os.getpid()}-{time.time_ns()}",
                policy=self._archive_policy(policy),
            ),
            control,
        )
