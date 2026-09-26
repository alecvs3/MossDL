"""Adapter contract shared by every client, plus completion detection helpers.

An adapter only *drives* a client (submit, relaunch, kill) and optionally
reports the client's own view of completion. The runner decides when a trial
is complete from evidence it controls: files at their final names with the
expected size, no temp siblings, the harness server idle, then SHA-256.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

# Temp/sidecar names used while a download is in flight. A final-named file
# with one of these siblings is not complete (aria2 pre-allocates the final
# name at full size, so size alone proves nothing).
TEMP_SUFFIXES = (".part", ".part.ranges.json", ".aria2", ".crdownload", ".download",
                 ".fdmdownload", ".tmp", ".segments.json", ".jdpart")


@dataclass
class JobFile:
    name: str
    url: str
    size: int                  # 0 when unknown (public files without --public-size)
    sha256: str | None


@dataclass
class Job:
    kind: str                          # http | hls | public
    files: list[JobFile]
    dest: Path
    policy: str                        # defaults | equalized
    connections: int | None = None     # per-file connections under "equalized"
    max_concurrent: int | None = None  # concurrent downloads under "equalized"
    extra: dict = field(default_factory=dict)


class Unavailable(RuntimeError):
    """The client is not installed/configured; the trial is recorded as unsupported."""


class Adapter:
    name = "abstract"            # CLI id
    display_name = "Abstract"    # label used in reports
    automation = "manual"        # api | cli | folderwatch | manual
    process_names: list[str] = []  # executable names the sampler attaches to

    def __init__(self, options: dict | None = None) -> None:
        self.options = dict(options or {})
        self._pids: list[int] = []

    # -- identity / capability ---------------------------------------------
    def version(self) -> str | None:
        return self.options.get("version")

    def check(self) -> None:
        """Raise Unavailable with a human-readable reason if the client can't run."""

    def unsupported_reason(self, job: Job) -> str | None:
        """Return why this client cannot do the job (e.g. no HLS), else None."""
        return None

    def applied_settings(self, job: Job) -> dict:
        """Settings in force for the trial, as applied by the adapter or confirmed by the operator."""
        return {"connections_per_file": job.connections, "max_concurrent": job.max_concurrent,
                "confirmed_by": "operator" if job.policy == "equalized" else "defaults"}

    # -- driving -------------------------------------------------------------
    def prepare(self, job: Job) -> None:
        """Untimed: bring the client to a warm, idle state (GUI apps: already running)."""

    def start(self, job: Job) -> None:
        """Timed from here: submit every file in ``job``."""
        raise NotImplementedError

    def poll(self) -> dict | None:
        """Client-reported status, e.g. {"done": True} or {"failed": "reason"}; None if unknown."""
        return None

    def pids(self) -> list[int]:
        return list(self._pids)

    def kill(self) -> None:
        """Hard-kill the client (resume scenario): its own processes and any by name."""
        victims = []
        for pid in self._pids:
            try:
                root = psutil.Process(pid)
                victims += [root, *root.children(recursive=True)]
            except psutil.Error:
                continue
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() in {n.lower() for n in self.process_names}:
                victims.append(proc)
        for proc in victims:
            try:
                proc.kill()
            except psutil.Error:
                continue
        psutil.wait_procs(victims, timeout=10)

    def relaunch(self, job: Job) -> None:
        """Bring the client back after kill() and make it resume the job."""
        raise Unavailable(f"{self.display_name}: relaunch/resume must be performed manually")

    def cleanup(self) -> None:
        """Remove finished entries from the client's list so the next trial starts clean."""


# ---- completion evidence -------------------------------------------------
def pending_files(job: Job, remaining: set[str]) -> set[str]:
    """Names in ``remaining`` that are not yet final at the expected size."""
    still = set()
    for spec in job.files:
        if spec.name not in remaining:
            continue
        target = job.dest / spec.name
        try:
            size = target.stat().st_size
        except OSError:
            still.add(spec.name)
            continue
        if (spec.size and size != spec.size) or (not spec.size and size == 0):
            still.add(spec.name)
            continue
        if any(os.path.exists(str(target) + suffix) for suffix in TEMP_SUFFIXES):
            still.add(spec.name)
    return still


def unexpected_files(job: Job) -> list[str]:
    expected = {spec.name for spec in job.files}
    try:
        present = {entry.name for entry in os.scandir(job.dest) if entry.is_file()}
    except OSError:
        return []
    return sorted(present - expected)


def wait_quiet(paths: list[Path], quiet_s: float = 1.0, timeout_s: float = 30.0) -> bool:
    """True once size+mtime of every path is unchanged for ``quiet_s`` seconds."""
    deadline = time.time() + timeout_s
    last = None
    stable_since = time.time()
    while time.time() < deadline:
        try:
            now = [(p.stat().st_size, p.stat().st_mtime_ns) for p in paths]
        except OSError:
            now = None
        if now is not None and now == last:
            if time.time() - stable_since >= quiet_s:
                return True
        else:
            stable_since = time.time()
        last = now
        time.sleep(0.1)
    return False
