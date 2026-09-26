"""MossDL adapter: runs drivers/mossdl_driver.py as a child process.

Options (``--opt key=value`` on the runner):
  mode=core|engine    default core (see the driver docstring for what each covers)
  core_path=...       explicit transfer-core.exe (else TRANSFER_CORE_PATH / repo build)
  repo=...            MossDL checkout (default: the repo containing docs/benchmarks)

Honesty notes recorded with every result:
  * core mode measures the shipped Rust transfer core with production defaults,
    not the Tauri UI; engine mode adds the Python engine (queue, resolution,
    persistence). Neither includes the WebView UI -- measure that separately
    with the idle-footprint scenario against the installed app.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .base import Adapter, Job, Unavailable

HARNESS_PARENT = Path(__file__).resolve().parents[2]      # docs/benchmarks
DEFAULT_REPO = Path(__file__).resolve().parents[4]        # repository root


class MossDLAdapter(Adapter):
    name = "mossdl"
    display_name = "MossDL"
    automation = "api"
    # Installed app, for the idle-footprint scenario (WebView2 children are
    # picked up as descendants of MossDL.exe).
    idle_process_names = ["MossDL.exe", "transfer-engine.exe", "transfer-core.exe"]

    def __init__(self, options: dict | None = None) -> None:
        super().__init__(options)
        self.mode = self.options.get("mode", "core")
        self.repo = Path(self.options.get("repo") or DEFAULT_REPO)
        self._proc: subprocess.Popen | None = None
        self._events: queue.Queue = queue.Queue()
        self._status: dict | None = None
        self._job_path: Path | None = None
        self._data_dir: Path | None = None
        self._ready = threading.Event()

    # -- identity -----------------------------------------------------------
    def _core_path(self) -> Path | None:
        configured = self.options.get("core_path") or os.environ.get("TRANSFER_CORE_PATH")
        candidates = [Path(configured)] if configured else []
        candidates += [self.repo / "src-tauri" / "target" / "release" / "transfer-core.exe",
                       self.repo / "src-tauri" / "target" / "release" / "transfer-core"]
        return next((path for path in candidates if path.is_file()), None)

    def version(self) -> str | None:
        try:
            package = json.loads((self.repo / "package.json").read_text(encoding="utf-8"))
            base = str(package.get("version"))
        except (OSError, ValueError):
            base = "unknown"
        core = self._core_path()
        stamp = f"core {core.stat().st_mtime:.0f}" if core else "no core"
        return f"{base} ({self.mode}; {stamp})"

    def check(self) -> None:
        if not (self.repo / "engine").is_dir():
            raise Unavailable(f"MossDL repo not found at {self.repo}")
        if self._core_path() is None:
            raise Unavailable("transfer-core binary not built (cargo build --release --bin transfer-core)")

    def unsupported_reason(self, job: Job) -> str | None:
        return None  # http, hls (engine media pipeline) and public URLs are all supported

    def applied_settings(self, job: Job) -> dict:
        settings = {"connections_per_file": job.connections or 8, "max_concurrent": job.max_concurrent or 6,
                    "min_segment_size": 16 * 1024 * 1024, "mode": self.mode,
                    "confirmed_by": "adapter" if job.policy == "equalized" else "defaults"}
        if self.mode == "engine" and job.files:
            settings["learned_host_profile"] = self._learned_profile(urlsplit(job.files[0].url).hostname or "")
        return settings

    def _learned_profile(self, host: str) -> dict | None:
        """The engine persists per-host concurrency ceilings in <repo>/logs, outside the
        data dir, so earlier runs can throttle this one. Record what it holds for the host."""
        path = self.repo / "logs" / "host_concurrency_profiles.json"
        try:
            profile = json.loads(path.read_text(encoding="utf-8")).get(host.lower())
        except (OSError, ValueError):
            return None
        return None if profile is None else {"verified_ceiling": profile.get("verified_ceiling"),
                                             "ceiling_locked": profile.get("ceiling_locked"), "source": str(path)}

    # -- driving ------------------------------------------------------------
    def _spawn(self, job: Job, resume: bool) -> None:
        env = dict(os.environ, MOSSDL_REPO=str(self.repo), PYTHONUNBUFFERED="1")
        core = self._core_path()
        if core:
            env["TRANSFER_CORE_PATH"] = str(core)
        command = [sys.executable, "-m", "harness.drivers.mossdl_driver", "--job", str(self._job_path),
                   "--mode", self.mode, "--data-dir", str(self._data_dir)]
        if resume:
            command.append("--resume")
        self._status = None
        self._events = queue.Queue()
        self._ready.clear()
        self._proc = subprocess.Popen(command, cwd=str(HARNESS_PARENT), env=env, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                      encoding="utf-8", errors="replace",
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._pids = [self._proc.pid]
        threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()

    def _read(self, proc: subprocess.Popen) -> None:
        tail: list[str] = []
        for line in proc.stdout:  # type: ignore[union-attr]
            if line.startswith("@@BENCH ") and proc is self._proc:
                event = json.loads(line[8:])
                self._events.put(event)
                if event["event"] == "ready":
                    self._ready.set()
                elif event["event"] == "done":
                    self._status = {"done": bool(event.get("ok")),
                                    **({} if event.get("ok") else {"failed": "driver reported file errors"})}
                elif event["event"] == "error":
                    self._status = {"failed": event.get("message")}
            else:
                tail = (tail + [line.rstrip()])[-20:]
        code = proc.wait()
        if proc is self._proc and self._status is None:  # a killed predecessor must not report
            self._status = {"failed": f"driver exited {code}: " + " | ".join(tail)[-800:]}
        self._ready.set()  # never leave prepare() waiting on a dead driver

    def prepare(self, job: Job) -> None:
        """Start MossDL and wait until it is warm and idle; the clock has not started yet."""
        work = job.dest.parent
        self._job_path = work / "_mossdl_job.json"
        self._data_dir = work / "_mossdl_data"
        shutil.rmtree(self._data_dir, ignore_errors=True)
        payload = {"kind": job.kind, "dest": str(job.dest), "connections": job.connections,
                   "max_concurrent": job.max_concurrent,
                   "files": [{"name": f.name, "url": f.url, "size": f.size} for f in job.files]}
        self._job_path.write_text(json.dumps(payload), encoding="utf-8")
        self._spawn(job, resume=False)
        if not self._ready.wait(timeout=180) or self._status:
            raise RuntimeError(f"MossDL driver not ready: {self._status}")

    def start(self, job: Job) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write("go\n")
        self._proc.stdin.flush()

    def relaunch(self, job: Job) -> None:
        self._spawn(job, resume=True)

    def poll(self) -> dict | None:
        return self._status

    def events(self) -> list[dict]:
        items = []
        while not self._events.empty():
            items.append(self._events.get_nowait())
        return items

    def cleanup(self) -> None:
        """Release the driver after the runner's final sample (it idles until told to exit)."""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.write("exit\n")  # type: ignore[union-attr]
                self._proc.stdin.flush()  # type: ignore[union-attr]
            except OSError as exc:
                print(f"[mossdl] could not signal driver exit: {exc}")
            self._proc.wait(timeout=60)
