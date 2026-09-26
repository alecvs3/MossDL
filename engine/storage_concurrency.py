"""Storage Host Concurrency Manager and Resizable Semaphore Controller (PIPE-05, PIPE-06).

Tracks download limits by storage host domain (e.g. tunnel5.dlproxy.uk vs datanodes.to),
mirrors { max_concurrent_streams, calibrated_at } to hosters.json in app-data, enforces
limits via a waiter-safe resizable semaphore, consults the authoritative host pressure
breaker, and detects 0-byte secondary stream stalls. hosters.json is a mirror of the
calibrated storage limits; the auditor's persisted host profile is the source of truth
for the breaker/ceiling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .telemetry import telemetry_bus

# How long an optimistic probe allowance stays open. Long enough for the
# auditor's ironclad rule (>1 MB and >5 s on every stream) to confirm it,
# short enough that a host which quietly ignores the extra stream reverts.
_PROBE_WINDOW_SECONDS = 120.0

# A stream below this many bytes has not started for real: hosts that queue
# surplus connections still send a few KB of preamble (measured: 7,835 bytes)
# before withholding the body, so "exactly zero" never fires.
_STARVED_STREAM_BYTES = 1024 * 1024
# Bytes/sec above which a stream counts as genuinely moving data.
_PRODUCTIVE_STREAM_BPS = 256 * 1024

# Dead-stream policy. A slow lane is a normal DataNodes serving state and will
# finish, so every one of these is set to make killing rare: only a stream
# sustaining under _STALL_MIN_BPS across a full window, past the grace period,
# big enough to measure, not nearly finished, and not already killed once.
_STALL_MIN_BPS = 512 * 1024
_STALL_GRACE_SECONDS = 90.0
_STALL_WINDOW_SECONDS = 60.0
_STALL_SAFE_FRACTION = 0.80
_STALL_MIN_FILE_BYTES = 50 * 1024 * 1024
_STALL_MAX_KILLS = 1

logger = logging.getLogger(__name__)


@dataclass
class StorageHostProfile:
    host: str
    max_concurrent_streams: int = 1
    calibrated_at: float = 0.0


@dataclass
class ActiveStream:
    task_id: str
    storage_host: str
    started_at: float = field(default_factory=time.time)
    last_bytes: int = 0
    total_bytes: int = 0
    last_update_at: float = field(default_factory=time.time)
    speed_bps: float = 0.0
    expected_bytes: int = 0
    # Rolling window, so a momentary dip cannot be mistaken for a dead stream.
    window_started_at: float = field(default_factory=time.time)
    window_start_bytes: int = 0
    window_samples: deque[tuple[float, int]] = field(default_factory=deque)
    sample_at: float = field(default_factory=time.time)
    sample_bytes: int = 0

    def window_rate_bps(self, now: float) -> tuple[float, float]:
        """(bytes/sec across the open window, seconds it covers)."""
        elapsed = max(0.0, now - self.window_started_at)
        if elapsed <= 0.0:
            return 0.0, 0.0
        moved = max(0, self.total_bytes - self.window_start_bytes)
        return moved / elapsed, elapsed


class ResizableSemaphore:
    """Waiter-safe resizable semaphore.

    Rebuilding an ``asyncio.Semaphore`` on limit changes strands waiters on the
    old object, and poking ``_value`` lets active holders exceed the cap. This
    limiter keeps one condition for every waiter, enforces capacity at wake-up
    time, and always notifies existing waiters on resize instead of dropping
    them.
    """

    def __init__(self, limit: int = 1) -> None:
        self._limit = max(1, int(limit))
        self._in_use = 0
        self._waiters = 0
        self._condition = asyncio.Condition()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def waiters(self) -> int:
        """Tasks blocked on this host right now: the real demand signal.

        Without it nothing could tell "this host is saturated and three more
        parts are queued" from "this host has one stream and nothing waiting",
        so a ceiling of 1 was never questioned.
        """
        return self._waiters

    def locked(self) -> bool:
        return self._in_use >= self._limit

    async def acquire(self) -> bool:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        async with self._condition:
            if self._in_use >= self._limit:
                self._waiters += 1
                try:
                    await self._condition.wait_for(lambda: self._in_use < self._limit)
                finally:
                    self._waiters -= 1
            self._in_use += 1
            return True

    async def release(self) -> None:
        async with self._condition:
            self._in_use = max(0, self._in_use - 1)
            self._condition.notify_all()

    def resize(self, limit: int) -> None:
        new_limit = max(1, int(limit))
        if new_limit == self._limit:
            return
        self._limit = new_limit
        self._wake_waiters()

    async def _notify_waiters(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    def _wake_waiters(self) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(self._notify_waiters())
        else:
            try:
                asyncio.run_coroutine_threadsafe(self._notify_waiters(), loop)
            except RuntimeError as err:
                telemetry_bus.record(
                    level="WARN",
                    subsystem="engine:storage",
                    message="[STORAGE_WAKE_FAILED] Could not schedule waiter notification on the engine loop",
                    context={"error": str(err)[:200], "loop_closed": loop.is_closed()},
                )

    async def __aenter__(self) -> "ResizableSemaphore":
        await self.acquire()
        return self

    async def __aexit__(self, _exc_type, _value, _traceback) -> None:
        await self.release()


class StorageHostConcurrencyManager:
    """Manages empirical storage host domain concurrency limits, semaphores, and hosters.json persistence."""

    def __init__(self, data_dir: Path | str, default_limit: int = 4, auditor: object = None) -> None:
        self.data_dir = Path(data_dir)
        self.default_limit = default_limit
        self.hosters_file = self.data_dir / "hosters.json"
        self._profiles: dict[str, StorageHostProfile] = {}
        self._semaphores: dict[str, ResizableSemaphore] = {}
        # host -> (probe_limit, expires_at): an optimistic allowance above the
        # calibrated limit, kept until the ratchet confirms it or it lapses.
        self._probe_allowance: dict[str, tuple[int, float]] = {}
        self._active_streams: dict[str, dict[str, ActiveStream]] = {}  # host -> {task_id: ActiveStream}
        # Kills survive the stream they ended: the whole point is that a task
        # gets at most one restart, and the restart creates a new ActiveStream.
        self._stall_kills: dict[str, int] = {}
        self._auditor = auditor
        self.load()

    def _auditor_ref(self):
        if self._auditor is not None:
            return self._auditor
        from .concurrency_auditor import concurrency_auditor
        return concurrency_auditor

    def _breaker_limit(self, storage_host: str) -> int | None:
        try:
            return self._auditor_ref().breaker_admission_limit(storage_host)
        except Exception as err:
            logger.warning("storage breaker admission lookup failed for %s: %s", storage_host, err)
            return None

    def load(self) -> None:
        """Load storage host limits from hosters.json."""
        if not self.hosters_file.exists():
            return
        try:
            raw = json.loads(self.hosters_file.read_text(encoding="utf-8") or "{}")
            for host, info in raw.items():
                if isinstance(info, dict):
                    clean = host.lower().strip()
                    self._profiles[clean] = StorageHostProfile(
                        host=clean,
                        max_concurrent_streams=max(1, int(info.get("max_concurrent_streams", 1))),
                        calibrated_at=float(info.get("calibrated_at", 0.0)),
                    )
        except Exception as exc:
            logger.warning("Failed to load hosters.json: %s", exc)

    def save(self) -> None:
        """Persist current storage host profiles to hosters.json."""
        try:
            data = {
                prof.host: {
                    "max_concurrent_streams": prof.max_concurrent_streams,
                    "calibrated_at": prof.calibrated_at,
                }
                for prof in self._profiles.values()
            }
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.hosters_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save hosters.json: %s", exc)

    def _stored_limit(self, clean: str, ttl: float) -> int:
        prof = self._profiles.get(clean)
        is_dn = clean == "datanodes.to" or clean.endswith(".datanodes.to")
        env_override = None
        if is_dn and os.environ.get("STORAGE_CONCURRENCY_DATANODES"):
            try:
                env_override = max(1, int(os.environ["STORAGE_CONCURRENCY_DATANODES"]))
            except ValueError:
                pass

        if not prof:
            if env_override is not None:
                return env_override
            return self.default_limit
        if prof.calibrated_at > 0 and (time.time() - prof.calibrated_at > ttl):
            # Calibration expired; return default to allow re-probing
            if env_override is not None:
                return env_override
            return self.default_limit
        return prof.max_concurrent_streams

    def get_limit(self, storage_host: str, ttl: float = 86400.0) -> int:
        """Effective limit: calibrated mirror, raised by an open probe, breaker-clamped.

        Order matters. The probe is applied BEFORE the breaker clamp so a host
        that refuses the extra stream is still vetoed back down; applying it
        afterwards would let a probe override an active freeze.
        """
        clean = storage_host.lower().strip()
        limit = self._stored_limit(clean, ttl)
        probe = self._probe_allowance.get(clean)
        if probe is not None:
            probe_limit, expires_at = probe
            if time.time() < expires_at:
                limit = max(limit, int(probe_limit))
            else:
                self._probe_allowance.pop(clean, None)
        breaker_limit = self._breaker_limit(clean)
        if breaker_limit is not None:
            limit = max(1, min(limit, breaker_limit))
        return limit

    def reconsider_limits(self) -> None:
        """Open an upward probe on any saturated host that has work waiting.

        This is the escape from the ladder's deadlock: `verified_ceiling` is only
        raised by observing MORE active streams than the ceiling, but the ceiling
        is what stops them starting. Nothing ever tested a healthy host, so a
        host calibrated to 1 stayed at 1 indefinitely while parts queued behind
        it. Called from the engine's periodic loop.
        """
        auditor = self._auditor_ref() if self._auditor_ref is not None else None
        if auditor is None or not hasattr(auditor, "probe_allowance"):
            return
        for host, sem in list(self._semaphores.items()):
            waiting = sem.waiters
            if waiting <= 0:
                continue
            # Demand alone is not a reason to open another lane, but "not fast"
            # is not a reason to refuse one either. Withholding whenever an open
            # stream fell below the productive threshold pinned the ceiling on
            # DataNodes, where slow lanes are normal and permanent: there was
            # always a slow stream, so a probe never fired. Only a genuinely
            # DEAD stream argues against opening another lane.
            open_streams = len(self._active_streams.get(host, {}))
            dead = self.dead_stream_count(host)
            if dead > 0:
                telemetry_bus.record(
                    level="DEBUG",
                    subsystem="engine:concurrency",
                    message=(
                        f"[STORAGE_PROBE_WITHHELD] {host} has {dead} of {open_streams} open stream(s) "
                        f"making no progress at all; not opening another lane"
                    ),
                    context={"host": host, "open_streams": open_streams,
                             "dead": dead, "waiting": waiting},
                    tier="engine",
                )
                continue
            allowed = auditor.probe_allowance(host, sem.limit, waiting)
            if allowed <= sem.limit:
                continue
            self._probe_allowance[host] = (allowed, time.time() + _PROBE_WINDOW_SECONDS)
            sem.resize(allowed)
            telemetry_bus.record(
                level="INFO",
                subsystem="engine:concurrency",
                message=(
                    f"[STORAGE_PROBE_OPENED] {host} raised {sem.limit - 1} -> {allowed} stream(s); "
                    f"{waiting} task(s) waiting"
                ),
                context={"host": host, "previous_limit": allowed - 1, "limit": allowed,
                         "waiting": waiting},
                tier="engine",
            )

    def set_limit(self, storage_host: str, limit: int, calibrated: bool = True) -> None:
        clean = storage_host.lower().strip()
        limit = max(1, limit)
        now = time.time() if calibrated else 0.0
        prof = self._profiles.setdefault(clean, StorageHostProfile(host=clean))
        prof.max_concurrent_streams = limit
        if calibrated:
            prof.calibrated_at = now
        self.save()
        sem = self.get_semaphore(clean)
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:storage_concurrency",
            message=(
                f"[STORAGE_HOST_LIMIT] Set {clean} max_concurrent_streams={limit} "
                f"(effective={sem.limit}, breaker={self._breaker_limit(clean)})"
            ),
            context={
                "storage_host": clean,
                "max_concurrent_streams": limit,
                "effective_limit": sem.limit,
                "calibrated_at": now,
            },
            tier="engine",
        )

    def get_semaphore(self, storage_host: str) -> ResizableSemaphore:
        clean = storage_host.lower().strip()
        limit = self.get_limit(clean)
        sem = self._semaphores.get(clean)
        if sem is None:
            sem = ResizableSemaphore(limit)
            self._semaphores[clean] = sem
        else:
            sem.resize(limit)
        return sem

    def register_stream(self, storage_host: str, task_id: str, expected_bytes: int = 0) -> None:
        clean = storage_host.lower().strip()
        streams = self._active_streams.setdefault(clean, {})
        streams[task_id] = ActiveStream(
            task_id=task_id, storage_host=clean, expected_bytes=max(0, int(expected_bytes or 0)))
        stream = streams[task_id]
        stream.window_samples.append((stream.window_started_at, 0))

    def update_stream(self, storage_host: str, task_id: str, bytes_transferred: int, speed: float = 0.0,
                      expected_bytes: int = 0) -> None:
        clean = storage_host.lower().strip()
        stream = self._active_streams.get(clean, {}).get(task_id)
        if stream:
            stream.last_bytes = stream.total_bytes
            stream.total_bytes = bytes_transferred
            stream.speed_bps = speed
            stream.last_update_at = time.time()
            if expected_bytes:
                stream.expected_bytes = max(0, int(expected_bytes))
            now = stream.last_update_at
            stream.window_samples.append((now, stream.total_bytes))
            cutoff = now - _STALL_WINDOW_SECONDS
            # Preserve the last sample at or before the cutoff as the baseline.
            # Resetting at exactly 60s made every watchdog check see a fresh,
            # zero-length window, so a truly dead stream could never qualify.
            while len(stream.window_samples) > 1 and stream.window_samples[1][0] <= cutoff:
                stream.window_samples.popleft()
            stream.window_started_at, stream.window_start_bytes = stream.window_samples[0]

    def unregister_stream(self, storage_host: str, task_id: str) -> None:
        clean = storage_host.lower().strip()
        if clean in self._active_streams:
            self._active_streams[clean].pop(task_id, None)
            if not self._active_streams[clean]:
                del self._active_streams[clean]

    def check_zero_byte_stalls(self, storage_host: str, stall_threshold: float = _STALL_GRACE_SECONDS) -> list[str]:
        """Task ids of streams that are genuinely dead, NOT merely slow.

        The earlier version of this treated "slow while a sibling is fast" as
        proof the host had accepted a stream and then starved it, and returned it
        to be dropped and requeued. That was wrong, and it was the reason a
        multipart package collapsed to one stream at a time.

        DataNodes' CDN assigns a lane per session. Some lanes are slow. A slow
        lane still finishes, and because the lane is bound to the session, a
        dropped stream re-resolves onto THE SAME slow lane -- so killing it costs
        a full re-solve and buys nothing. Slowness here is a normal serving
        state, not a host refusing to serve.

        The policy is therefore deliberately reluctant, and matches what a
        known-good DataNodes client settled on empirically: judge sustained rate
        over a window rather than bytes-so-far, allow a long grace, never touch a
        stream that is nearly done or too small to measure, and kill any given
        stream at most once. Callers must NOT demote or lock the host's ceiling
        on the strength of this signal.
        """
        clean = storage_host.lower().strip()
        streams = self._active_streams.get(clean, {})
        if len(streams) < 2:
            return []

        now = time.time()
        # A productive sibling is the proof the host is willing to serve us at
        # all; without one, everything being slow is a different problem.
        def moving(stream: ActiveStream) -> bool:
            rate, _window = stream.window_rate_bps(now)
            recent_speed = (
                now - stream.last_update_at <= 5.0
                and stream.speed_bps >= _PRODUCTIVE_STREAM_BPS
            )
            return recent_speed or rate >= _PRODUCTIVE_STREAM_BPS

        if not any(moving(stream) for stream in streams.values()):
            return []

        grace = max(float(stall_threshold or 0.0), _STALL_GRACE_SECONDS)
        stalled_task_ids: list[str] = []
        for task_id, s in streams.items():
            if self._stall_kills.get(task_id, 0) >= _STALL_MAX_KILLS:
                continue  # already given one chance; let it run to completion
            if now - s.started_at < grace:
                continue
            if s.expected_bytes and s.expected_bytes < _STALL_MIN_FILE_BYTES:
                continue  # too small to tell a slow lane from a short file
            if s.expected_bytes and s.total_bytes >= s.expected_bytes * _STALL_SAFE_FRACTION:
                continue  # nearly done; restarting would throw away real work
            rate, window = s.window_rate_bps(now)
            if window < _STALL_WINDOW_SECONDS:
                continue  # not enough history to judge
            if rate >= _STALL_MIN_BPS:
                continue
            stalled_task_ids.append(task_id)

        for task_id in stalled_task_ids:
            self._stall_kills[task_id] = self._stall_kills.get(task_id, 0) + 1

        return stalled_task_ids

    def dead_stream_count(self, storage_host: str) -> int:
        """Open streams past the grace period sustaining essentially nothing.

        Distinct from "not productive": a lane running at 200 KB/s is slow, not
        dead, and will finish. Only a stream below _STALL_MIN_BPS across a full
        window counts here.
        """
        clean = storage_host.lower().strip()
        now = time.time()
        dead = 0
        for s in self._active_streams.get(clean, {}).values():
            if now - s.started_at < _STALL_GRACE_SECONDS:
                continue
            rate, window = s.window_rate_bps(now)
            if window < _STALL_WINDOW_SECONDS:
                continue
            if rate < _STALL_MIN_BPS:
                dead += 1
        return dead

    def productive_stream_count(self, storage_host: str) -> int:
        """Streams actually moving bytes right now.

        The ladder must ramp on this, not on how many streams were opened: the
        engine counted three "active" streams while only one was transferring.
        """
        clean = storage_host.lower().strip()
        return sum(
            1 for s in self._active_streams.get(clean, {}).values()
            if s.speed_bps >= _PRODUCTIVE_STREAM_BPS
        )

    def aggregate_speed_bps(self, storage_host: str) -> float:
        """Total bytes/sec across this host's streams -- the number to optimise."""
        clean = storage_host.lower().strip()
        return sum(float(s.speed_bps or 0.0) for s in self._active_streams.get(clean, {}).values())

    def sample_host_goodput(self, storage_host: str, *, now: float | None = None) -> dict[str, object]:
        """Measure host goodput from cumulative bytes over one shared interval.

        Callback EMAs arrive at different times and remain stale while a lane is
        silent, so summing them cannot describe what the host moved during a
        wall-clock interval. This sampler advances every stream on the same tick.
        """
        clean = storage_host.lower().strip()
        sampled_at = time.time() if now is None else float(now)
        rows: list[dict[str, object]] = []
        aggregate = 0.0
        productive = 0
        for task_id, stream in self._active_streams.get(clean, {}).items():
            elapsed = max(0.001, sampled_at - stream.sample_at)
            moved = max(0, int(stream.total_bytes) - int(stream.sample_bytes))
            speed = moved / elapsed
            stream.sample_at = sampled_at
            stream.sample_bytes = int(stream.total_bytes)
            aggregate += speed
            if speed >= _PRODUCTIVE_STREAM_BPS:
                productive += 1
            rows.append({
                "stream": task_id,
                "bytes": int(stream.total_bytes),
                "interval_bytes": moved,
                "interval_seconds": elapsed,
                "speed_bps": speed,
            })
        return {
            "host": clean,
            "sampled_at": sampled_at,
            "open": len(rows),
            "productive": productive,
            "aggregate_speed_bps": aggregate,
            "streams": rows,
        }
