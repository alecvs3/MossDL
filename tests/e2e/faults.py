"""Deterministic fault seams for the offline lifecycle matrix (master plan wave 5).

Three failure modes the happy-path matrix never exercises live here:

* ``RangeFaultPolicy`` -- HTTP 429/403 + ``Retry-After`` backpressure on range
  requests, counter driven (never wall-clock racy) so a scenario throttles the
  same requests on every run.
* ``missing_volumes`` -- contiguity check used by the fake archive backend so a
  package with a hole behaves like a real extractor instead of silently
  succeeding.
* ``UnlinkFault`` -- a transient (or permanent) ``WinError 32`` sharing
  violation on source-volume cleanup.

``ProgressWatcher`` samples the artifacts the assertions need while a scenario
runs: ``.part`` survival, host window permits, and segment permits.  Every seam
records structured evidence; nothing here swallows a condition silently.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

WINDOWS_SHARING_VIOLATION = 32
_LOCK_MESSAGE = "The process cannot access the file because it is being used by another process"
_PART_VOLUME = re.compile(r"^(?P<base>.+?)[._-]part(?P<num>\d+)\.rar$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 1. 429 / Retry-After backpressure
# --------------------------------------------------------------------------- #


@dataclass
class RangeFaultPolicy:
    """Throttle a deterministic slice of range requests with 429/403.

    ``after_requests`` range responses are served cleanly first so the ``.part``
    file always holds durable progress before the throttle lands; the next
    ``faults`` range requests answer ``status`` + ``Retry-After``.
    """

    after_requests: int = 1
    faults: int = 0
    status: int = 429
    retry_after: float = 1.0

    served: int = 0
    injected: int = 0
    windows: list[tuple[float, float]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def next_range_response(self) -> tuple[int, float] | None:
        """Return ``(status, retry_after)`` when this range request must fail."""
        with self._lock:
            self.served += 1
            if self.faults <= 0 or self.served <= self.after_requests:
                return None
            if self.injected >= self.faults:
                return None
            self.injected += 1
            self.windows.append((time.monotonic(), self.retry_after))
            return self.status, self.retry_after

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "range_requests_served": self.served,
                "faults_injected": self.injected,
                "status": self.status,
                "retry_after": self.retry_after,
            }


# --------------------------------------------------------------------------- #
# 2. Missing archive volume
# --------------------------------------------------------------------------- #


def missing_volumes(first_volume: Path) -> list[int]:
    """Part numbers missing from the ``.partN.rar`` set containing ``first_volume``.

    Mirrors what a real extractor reports ("cannot find volume ...part3.rar")
    instead of the fake backend cheerfully extracting an incomplete package.
    """
    match = _PART_VOLUME.match(first_volume.name)
    if not match:
        return []
    base = match.group("base")
    present: set[int] = set()
    for sibling in first_volume.parent.glob("*.rar"):
        found = _PART_VOLUME.match(sibling.name)
        if found and found.group("base").casefold() == base.casefold():
            present.add(int(found.group("num")))
    if not present:
        return []
    return [number for number in range(1, max(present) + 1) if number not in present]


# --------------------------------------------------------------------------- #
# 3. Transient Windows file lock during cleanup
# --------------------------------------------------------------------------- #


class UnlinkFault:
    """Raise ``WinError 32`` for matching paths on their first N unlink attempts.

    Patching the ``Path.unlink`` seam (rather than holding a real handle) keeps
    the injection deterministic: the historical bug was a single unlink whose
    ``OSError`` was swallowed while the job still reported success, so the fault
    must fire on attempt 1 of every source volume, every run.
    """

    def __init__(self, match: Callable[[Path], bool], failures: int = 1,
                 permanent: bool = False) -> None:
        self._match = match
        self._failures = max(0, int(failures))
        self._permanent = bool(permanent)
        self._lock = threading.Lock()
        self._per_path: dict[str, int] = {}
        self.attempts = 0
        self.injected = 0

    def _should_fail(self, path: Path) -> bool:
        key = str(path)
        with self._lock:
            self.attempts += 1
            seen = self._per_path.get(key, 0) + 1
            self._per_path[key] = seen
            if self._permanent or seen <= self._failures:
                self.injected += 1
                return True
        return False

    @contextmanager
    def installed(self) -> Iterator["UnlinkFault"]:
        original = Path.unlink
        fault = self

        def patched(self: Path, missing_ok: bool = False) -> None:
            if fault._match(self) and fault._should_fail(self):
                raise OSError(13, _LOCK_MESSAGE, str(self), WINDOWS_SHARING_VIOLATION)
            return original(self, missing_ok=missing_ok)

        Path.unlink = patched  # type: ignore[assignment]
        try:
            yield self
        finally:
            Path.unlink = original  # type: ignore[assignment]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {"unlink_attempts": self.attempts, "lock_failures_injected": self.injected,
                    "permanent": self._permanent, "failures_per_path": self._failures}


# --------------------------------------------------------------------------- #
# Runtime sampling: .part survival + permit release during backoff
# --------------------------------------------------------------------------- #


class ProgressWatcher(threading.Thread):
    """Sample ``.part`` files and transfer permits while a scenario runs."""

    def __init__(self, destination: Path, resources: Any, host: str,
                 interval: float = 0.02) -> None:
        super().__init__(daemon=True)
        self.destination = Path(destination)
        self.resources = resources
        self.host = host
        self.interval = interval
        self._stop = threading.Event()
        self.samples: list[dict[str, Any]] = []
        self.part_deletions: list[dict[str, Any]] = []
        self.publish_races: list[dict[str, Any]] = []
        self.max_part_bytes: dict[str, int] = {}
        self.initial_segment_permits = self._segment_permits()

    def _segment_permits(self) -> int:
        semaphore = getattr(self.resources, "active_segments", None)
        return int(getattr(semaphore, "_value", 0) or 0)

    def _host_in_flight(self) -> int:
        window = (getattr(self.resources, "hosts", None) or {}).get(self.host)
        return int(getattr(window, "in_flight", 0) or 0)

    def run(self) -> None:  # noqa: D102 - thread body
        while not self._stop.is_set():
            self._tick()
            time.sleep(self.interval)
        self._tick()

    def _tick(self) -> None:
        sizes: dict[str, int] = {}
        if self.destination.exists():
            for path in self.destination.glob("*.part"):
                try:
                    sizes[path.name] = path.stat().st_size
                except OSError as err:
                    # The part vanished between glob and stat. That is a normal
                    # atomic publish when the finished file now exists; anything
                    # else is the data-loss event this watcher hunts for.
                    record = {"part": path.name, "reason": f"stat failed: {err}",
                              "at": round(time.monotonic(), 4)}
                    if (self.destination / path.name[: -len(".part")]).exists():
                        self.publish_races.append(record)
                    else:
                        self.part_deletions.append(record)
        for name, size in sizes.items():
            if size > self.max_part_bytes.get(name, 0):
                self.max_part_bytes[name] = size
        for name, best in list(self.max_part_bytes.items()):
            if best <= 0 or name in sizes:
                continue
            published = self.destination / name[: -len(".part")]
            if published.exists():
                continue
            self.part_deletions.append({
                "part": name, "reason": "part removed before its file was published",
                "max_bytes": best, "at": round(time.monotonic(), 4)})
            self.max_part_bytes[name] = 0
        self.samples.append({
            "at": time.monotonic(),
            "segment_permits_available": self._segment_permits(),
            "host_in_flight": self._host_in_flight(),
            "parts": sizes,
        })

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=5.0)

    def permits_released_during(self, start: float, duration: float) -> bool:
        """True when the transfer held no host/segment permit inside a window."""
        end = start + duration
        return any(
            start <= sample["at"] <= end
            and sample["host_in_flight"] == 0
            and sample["segment_permits_available"] >= self.initial_segment_permits
            for sample in self.samples
        )

    def summary(self) -> dict[str, Any]:
        return {
            "samples": len(self.samples),
            "part_files_seen": sorted(self.max_part_bytes),
            "max_part_bytes": dict(self.max_part_bytes),
            "part_deletions": list(self.part_deletions),
            "publish_races": list(self.publish_races),
            "initial_segment_permits": self.initial_segment_permits,
        }


# --------------------------------------------------------------------------- #
# Host pressure breaker isolation (the auditor is a process-wide singleton)
# --------------------------------------------------------------------------- #


@contextmanager
def isolated_auditor(workdir: Path, host: str) -> Iterator[Any]:
    """Point the singleton auditor at scenario-local files and clean up after.

    Without this a tripped breaker would persist into ``logs/`` and freeze the
    harness host (127.0.0.1) for every later scenario in the same process.
    """
    from engine.concurrency_auditor import concurrency_auditor as auditor

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    saved = (auditor.log_path, auditor.profiles_path, auditor.store)
    previous_profile = auditor._profiles.pop(host.lower(), None)
    auditor.log_path = workdir / "host_concurrency_audit.jsonl"
    auditor.profiles_path = workdir / "host_concurrency_profiles.json"
    try:
        yield auditor
    finally:
        auditor.reset_breaker(host, reason="lifecycle_harness_teardown")
        auditor._profiles.pop(host.lower(), None)
        if previous_profile is not None:
            auditor._profiles[host.lower()] = previous_profile
        auditor.log_path, auditor.profiles_path, auditor.store = saved


def expire_breaker_cooldown(host: str) -> float:
    """Fast-forward the breaker cooldown (>= 300 s by policy) for an offline run.

    Returns the number of seconds skipped so the scenario summary can state
    plainly that simulated time, not an assertion, was relaxed.
    """
    from engine.concurrency_auditor import concurrency_auditor as auditor

    profile = auditor.get_profile(host)
    now = time.time()
    skipped = max(0.0, max(profile.cooldown_until, profile.breaker.cooldown_until) - now)
    profile.cooldown_until = 0.0
    profile.breaker.cooldown_until = now - 1.0
    return round(skipped, 2)


def breaker_events(telemetry: list[Any], host: str) -> list[str]:
    """Breaker/ladder audit events seen for ``host``, in order."""
    found: list[str] = []
    for event in telemetry:
        context = getattr(event, "context", None) or {}
        if str(context.get("host", "")).lower() != host.lower():
            continue
        name = str(context.get("event", ""))
        if name:
            found.append(name)
    return found
