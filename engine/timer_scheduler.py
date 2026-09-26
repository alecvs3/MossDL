"""Cross-part hoster wait-timer scheduler.

Owns every hoster countdown/wait deadline for the engine:

* Arms a timer only after two consecutive decreasing samples (streaming DOM
  observation) so static decoy numbers cannot latch a wait.
* Skips arming entirely when clearance cookies, API keys, or a direct URL
  already satisfy the server-side wait (``[TIMER_SKIP]``).
* Keeps deadlines per ``(host, part)`` so multipart packages run their timers
  down concurrently instead of serially.
* Serializes only the final POST/trigger per host through a small host gate.
* Supports re-arming after a fresh-solve retry.
* Emits structured telemetry: ``[TIMER_CANDIDATES]``, ``[TIMER_ARMED]``,
  ``[TIMER_TICK]``, ``[TIMER_SKIP]``, ``[TIMER_SATISFIED_EARLY]``,
  ``[TIMER_RATE_LIMITED]``, ``[TIMER_PARSE_MISS]`` (plus ``[TIMER_CAPPED]``).

Waiting is always done through the cancellable async primitive
:meth:`TimerScheduler.wait_until`.  The sync facades
(:meth:`TimerScheduler.wait_for_sync` / :meth:`TimerScheduler.wait_until_sync`)
bridge onto a dedicated runtime event loop and never spin with ``time.sleep``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from .timer_detector import TimerCandidate, TimerDetector
from . import provider_wait

logger = logging.getLogger(__name__)

TELEMETRY_SUBSYSTEM = "engine:timer"


@dataclass(frozen=True)
class TimerArmState:
    """An armed deadline for a single ``(host, part)`` pair."""

    host: str
    key: str
    deadline: float
    seconds: int
    source: str
    confidence: float
    armed_at: float
    capped: bool = False
    reason: str = ""

    def remaining(self, now: float | None = None) -> float:
        return max(0.0, self.deadline - (time.monotonic() if now is None else now))

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "key": self.key,
            "seconds": self.seconds,
            "source": self.source,
            "confidence": self.confidence,
            "capped": self.capped,
            "reason": self.reason,
            "deadline_offset_s": round(self.deadline - self.armed_at, 3),
        }


class TimerScheduler:
    """Concurrency-aware wait-timer registry and async wait primitive."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        telemetry: Any = None,
        max_wait_seconds: float = 600.0,
        default_tick_interval: float = 1.0,
    ) -> None:
        self._clock = clock
        self._telemetry = telemetry
        self.max_wait_seconds = float(max_wait_seconds)
        self.default_tick_interval = float(default_tick_interval)

        self._lock = threading.RLock()
        self._arms: dict[tuple[str, str], TimerArmState] = {}
        self._samples: dict[tuple[str, str], list[int]] = {}
        self._host_gates: dict[str, threading.Lock] = {}
        self._host_async_locks: dict[tuple[int, str], asyncio.Lock] = {}

        self._runtime_lock = threading.Lock()
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._runtime_thread: threading.Thread | None = None
        self._runtime_ready = threading.Event()

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------
    def _emit(self, level: str, tag: str, message: str, context: Mapping[str, Any] | None = None) -> None:
        states = {'TIMER_ARMED': 'waiting', 'TIMER_TICK': 'waiting',
                  'TIMER_SKIP': 'finished', 'TIMER_SATISFIED_EARLY': 'finished'}
        if tag in states:
            payload = dict(context or {})
            payload.setdefault('countdown_seconds', payload.get('seconds'))
            finished = tag == 'TIMER_TICK' and payload.get('countdown_seconds') == 0
            provider_wait.publish('finished' if finished else states[tag], payload)
        bus = self._telemetry
        if bus is None:
            try:
                from .telemetry import telemetry_bus as bus  # local import avoids cycle
            except Exception:
                bus = None
        if bus is None:
            logger.debug("[%s] %s %s", tag, message, dict(context or {}))
            return
        try:
            bus.record(
                level=level,
                subsystem=TELEMETRY_SUBSYSTEM,
                message=f"[{tag}] {message}",
                context=dict(context or {}),
                tier="engine",
            )
        except Exception:
            logger.debug("[%s] %s (telemetry unavailable)", tag, message)

    # ------------------------------------------------------------------
    # Sampling / arming
    # ------------------------------------------------------------------
    def note_candidates(
        self,
        host: str,
        key: str,
        candidates: Sequence[TimerCandidate] | None,
        *,
        telemetry_ctx: Mapping[str, Any] | None = None,
    ) -> TimerCandidate | None:
        """Records the ranked candidate list and emits parse diagnostics."""
        cand_list = list(candidates or [])
        best = TimerDetector.best_candidate(cand_list)
        ctx = {**dict(telemetry_ctx or {}), "host": host, "key": str(key)}
        if cand_list:
            self._emit(
                "DEBUG" if best is not None else "WARN",
                "TIMER_CANDIDATES",
                (
                    f"{len(cand_list)} candidate(s); best={best.seconds}s ({best.source})"
                    if best is not None
                    else f"{len(cand_list)} candidate(s) but none valid"
                ),
                {
                    **ctx,
                    "count": len(cand_list),
                    "best_seconds": best.seconds if best is not None else None,
                    "best_source": best.source if best is not None else None,
                    "candidates": [c.to_dict() for c in cand_list[:8]],
                },
            )
        if best is None:
            self._emit(
                "WARN",
                "TIMER_PARSE_MISS",
                "no valid wait-timer candidate found",
                {**ctx, "count": len(cand_list)},
            )
        return best

    def skip(
        self,
        host: str,
        key: str,
        reason: str,
        *,
        telemetry_ctx: Mapping[str, Any] | None = None,
    ) -> None:
        """Disarms any pending timer and records why the wait is unnecessary."""
        with self._lock:
            self._arms.pop(self._key(host, key), None)
            self._samples.pop(self._key(host, key), None)
        self._emit(
            "INFO",
            "TIMER_SKIP",
            f"wait timer skipped: {reason}",
            {**dict(telemetry_ctx or {}), "host": host, "key": str(key), "reason": reason},
        )

    def observe(
        self,
        host: str,
        key: str,
        candidates: Sequence[TimerCandidate] | None,
        *,
        streaming: bool = True,
        satisfied_reason: str | None = None,
        telemetry_ctx: Mapping[str, Any] | None = None,
    ) -> TimerArmState | None:
        """Ingests one observation round.

        With ``streaming=True`` (live DOM polling) the timer arms only once two
        consecutive samples strictly decrease. With ``streaming=False`` (single
        HTTP response) the parsed candidate is trusted immediately.
        """
        best = self.note_candidates(host, key, candidates, telemetry_ctx=telemetry_ctx)
        if satisfied_reason:
            self.skip(host, key, satisfied_reason, telemetry_ctx=telemetry_ctx)
            return None
        if best is None:
            return None

        hk = self._key(host, key)
        with self._lock:
            existing = self._arms.get(hk)
            if existing is not None:
                return existing
            if not streaming:
                ready_to_arm = True
            else:
                samples = self._samples.setdefault(hk, [])
                samples.append(int(best.seconds))
                del samples[:-4]
                ready_to_arm = len(samples) >= 2 and samples[-1] < samples[-2]

        if not ready_to_arm:
            return None
        return self.arm(
            host,
            key,
            best.seconds,
            source=best.source,
            confidence=best.confidence,
            telemetry_ctx=telemetry_ctx,
            reason="observed",
        )

    def arm(
        self,
        host: str,
        key: str,
        seconds: float,
        *,
        source: str = "explicit",
        confidence: float = 1.0,
        telemetry_ctx: Mapping[str, Any] | None = None,
        reason: str = "armed",
    ) -> TimerArmState | None:
        """Registers a deadline starting now for ``(host, key)``."""
        try:
            secs = int(math.ceil(float(seconds)))
        except (TypeError, ValueError):
            secs = 0
        ctx = {**dict(telemetry_ctx or {}), "host": host, "key": str(key)}
        if secs <= 0:
            self._emit(
                "WARN",
                "TIMER_SKIP",
                f"non-positive wait timer ({seconds!r}) ignored",
                {**ctx, "requested_seconds": seconds, "reason": "non_positive"},
            )
            return None

        capped = False
        if self.max_wait_seconds > 0 and secs > self.max_wait_seconds:
            cap = int(self.max_wait_seconds)
            self._emit(
                "WARN",
                "TIMER_CAPPED",
                f"wait timer {secs}s exceeds local budget {cap}s; preserving server deadline",
                {**ctx, "requested_seconds": secs, "cap_seconds": cap},
            )
            capped = True

        now = self._clock()
        state = TimerArmState(
            host=host,
            key=str(key),
            deadline=now + secs,
            seconds=secs,
            source=source,
            confidence=float(confidence),
            armed_at=now,
            capped=capped,
            reason=reason,
        )
        with self._lock:
            self._arms[self._key(host, key)] = state
            self._samples.pop(self._key(host, key), None)
        self._emit(
            "WARN" if capped else "INFO",
            "TIMER_ARMED",
            f"timer armed: {secs}s from {source} (reason={reason})",
            {**ctx, **state.to_dict()},
        )
        return state

    def rearm(
        self,
        host: str,
        key: str,
        *,
        seconds: float | None = None,
        candidates: Sequence[TimerCandidate] | None = None,
        reason: str = "fresh_solve",
        telemetry_ctx: Mapping[str, Any] | None = None,
    ) -> TimerArmState | None:
        """Resets sampling and re-arms after a fresh solve / navigation."""
        with self._lock:
            self._arms.pop(self._key(host, key), None)
            self._samples.pop(self._key(host, key), None)
        source = "rearm"
        confidence = 1.0
        value = seconds
        if value is None and candidates:
            best = TimerDetector.best_candidate(list(candidates))
            if best is not None:
                value = best.seconds
                source = best.source
                confidence = best.confidence
        if value is None:
            self._emit(
                "WARN",
                "TIMER_PARSE_MISS",
                f"re-arm requested ({reason}) but no wait value available",
                {**dict(telemetry_ctx or {}), "host": host, "key": str(key), "reason": reason},
            )
            return None
        return self.arm(
            host,
            key,
            value,
            source=source,
            confidence=confidence,
            telemetry_ctx=telemetry_ctx,
            reason=reason,
        )

    def satisfied(
        self,
        host: str,
        key: str,
        reason: str,
        *,
        telemetry_ctx: Mapping[str, Any] | None = None,
    ) -> None:
        """Records that the server-side wait was satisfied before the deadline."""
        with self._lock:
            state = self._arms.pop(self._key(host, key), None)
            self._samples.pop(self._key(host, key), None)
        self._emit(
            "INFO",
            "TIMER_SATISFIED_EARLY",
            f"timer satisfied early: {reason}",
            {
                **dict(telemetry_ctx or {}),
                "host": host,
                "key": str(key),
                "reason": reason,
                "cleared_remaining_s": round(state.remaining(self._clock()), 2) if state else 0.0,
            },
        )

    def note_rate_limited(
        self,
        host: str,
        key: str,
        *,
        seconds: float | None = None,
        status: int | None = None,
        telemetry_ctx: Mapping[str, Any] | None = None,
    ) -> TimerArmState | None:
        """Records a host rate-limit response and optionally arms the retry wait."""
        self._emit(
            "WARN",
            "TIMER_RATE_LIMITED",
            f"host rate-limited (status={status}); retry wait={seconds}",
            {
                **dict(telemetry_ctx or {}),
                "host": host,
                "key": str(key),
                "status": status,
                "seconds": seconds,
            },
        )
        if seconds is not None and float(seconds) > 0:
            return self.arm(
                host,
                key,
                seconds,
                source="rate_limit",
                confidence=0.9,
                telemetry_ctx=telemetry_ctx,
                reason="rate_limited",
            )
        return None

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------
    def _key(self, host: str, key: str) -> tuple[str, str]:
        return ((host or "").lower(), provider_wait.scoped_key(key))

    def armed_state(self, host: str, key: str) -> TimerArmState | None:
        with self._lock:
            return self._arms.get(self._key(host, key))

    def is_armed(self, host: str, key: str) -> bool:
        return self.armed_state(host, key) is not None

    def remaining(self, host: str, key: str) -> float:
        state = self.armed_state(host, key)
        return state.remaining(self._clock()) if state else 0.0

    def armed_states(self) -> list[TimerArmState]:
        with self._lock:
            return list(self._arms.values())

    def clear(self, host: str, key: str) -> None:
        with self._lock:
            self._arms.pop(self._key(host, key), None)
            self._samples.pop(self._key(host, key), None)

    def clear_host(self, host: str) -> None:
        with self._lock:
            for hk in [k for k in self._arms if k[0] == (host or "").lower()]:
                self._arms.pop(hk, None)
                self._samples.pop(hk, None)

    # ------------------------------------------------------------------
    # Host POST serialization
    # ------------------------------------------------------------------
    def host_gate(self, host: str) -> threading.Lock:
        """Returns the canonical per-host trigger lock for sync callers."""
        normalized = (host or "").lower()
        with self._lock:
            lock = self._host_gates.get(normalized)
            if lock is None:
                lock = threading.Lock()
                self._host_gates[normalized] = lock
            return lock

    @asynccontextmanager
    async def async_host_gate(self, host: str) -> AsyncIterator[None]:
        """Async per-host trigger serialization (bound to the running loop)."""
        loop = asyncio.get_running_loop()
        marker = (id(loop), (host or "").lower())
        with self._lock:
            lock = self._host_async_locks.get(marker)
            if lock is None:
                lock = asyncio.Lock()
                self._host_async_locks[marker] = lock
        async with lock:
            yield

    # ------------------------------------------------------------------
    # Async wait primitive (cancellable, no thread blocking)
    # ------------------------------------------------------------------
    async def wait_until(
        self,
        deadline: float,
        *,
        host: str = "",
        key: str = "",
        telemetry_ctx: Mapping[str, Any] | None = None,
        tick_interval: float | None = None,
        cancel_event: Any = None,
        _follow_arm: bool = False,
    ) -> bool:
        """Awaits ``deadline`` (monotonic clock). Returns False if cancelled."""
        interval = float(tick_interval or self.default_tick_interval)
        cancel_event = cancel_event or provider_wait.operation.get()
        ctx = {**dict(telemetry_ctx or {}), "host": host, "key": str(key)}
        last_tick: int | None = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._emit('INFO', 'TIMER_SKIP', 'timer cancelled', {**ctx, 'reason': 'cancelled'})
                return False
            if _follow_arm:
                current = self.armed_state(host, key)
                if current is None:
                    return True
                deadline = current.deadline
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            tick = max(0, int(math.ceil(remaining)))
            if tick != last_tick:
                last_tick = tick
                self._emit(
                    "DEBUG",
                    "TIMER_TICK",
                    f"countdown {tick}s remaining",
                    {**ctx, "countdown_seconds": tick, "remaining_s": round(remaining, 2)},
                )
            if cancel_event is not None and cancel_event.is_set():
                self._emit(
                    "INFO",
                    "TIMER_SKIP",
                    "timer wait cancelled by caller",
                    {**ctx, "reason": "cancelled", "remaining_s": round(remaining, 2)},
                )
                return False
            await asyncio.sleep(min(interval, max(0.02, remaining)))
        if last_tick != 0:
            self._emit(
                "DEBUG",
                "TIMER_TICK",
                "countdown 0s remaining",
                {**ctx, "countdown_seconds": 0, "remaining_s": 0.0},
            )
        return True

    async def wait_for(
        self,
        host: str,
        key: str,
        *,
        telemetry_ctx: Mapping[str, Any] | None = None,
        tick_interval: float | None = None,
        cancel_event: Any = None,
    ) -> bool:
        """Awaits the armed deadline for ``(host, key)``; consumes it on success."""
        state = self.armed_state(host, key)
        if cancel_event is not None and cancel_event.is_set():
            return False
        if state is None:
            return True
        if state.remaining(self._clock()) <= 0:
            self.satisfied(host, key, "already_elapsed", telemetry_ctx=telemetry_ctx)
            return True
        completed = await self.wait_until(
            state.deadline,
            host=host,
            key=key,
            telemetry_ctx=telemetry_ctx,
            tick_interval=tick_interval,
            cancel_event=cancel_event,
            _follow_arm=True,
        )
        if completed:
            with self._lock:
                current = self._arms.get(self._key(host, key))
                if current is not None and current.deadline <= self._clock():
                    self._arms.pop(self._key(host, key), None)
                elif current is not None:
                    completed = False
        return completed

    # ------------------------------------------------------------------
    # Sync facades (for callers that cannot be made async this wave)
    # ------------------------------------------------------------------
    def _ensure_runtime_loop(self) -> asyncio.AbstractEventLoop:
        with self._runtime_lock:
            if self._runtime_loop is not None and not self._runtime_loop.is_closed() and self._runtime_loop.is_running():
                return self._runtime_loop
            if self._runtime_thread is None or not self._runtime_thread.is_alive():
                self._runtime_ready = threading.Event()
                ready = self._runtime_ready

                def _runner() -> None:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    with self._runtime_lock:
                        self._runtime_loop = loop
                        ready.set()
                    try:
                        loop.run_forever()
                    finally:
                        loop.close()

                self._runtime_thread = threading.Thread(target=_runner, name="TimerSchedulerRuntime", daemon=True)
                self._runtime_thread.start()
            else:
                ready = self._runtime_ready

        if not ready.wait(timeout=5.0):
            raise RuntimeError("TimerScheduler runtime loop failed to start within 5s")
        with self._runtime_lock:
            if self._runtime_loop is None:
                raise RuntimeError("TimerScheduler runtime loop unavailable")
            return self._runtime_loop

    def _run_sync(self, coro: Any, budget: float | None, context: Mapping[str, Any]) -> bool:
        loop = self._ensure_runtime_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return bool(future.result(timeout=budget))
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._emit("WARN", "TIMER_SKIP", "sync wait timed out", {**dict(context), "reason": "sync_wait_timeout"})
            return False
        except Exception as exc:
            self._emit("WARN", "TIMER_SKIP", f"sync wait failed: {exc}", {**dict(context), "reason": "sync_wait_error"})
            return False

    def wait_until_sync(
        self,
        deadline: float,
        *,
        host: str = "",
        key: str = "",
        telemetry_ctx: Mapping[str, Any] | None = None,
        tick_interval: float | None = None,
        cancel_event: Any = None,
        timeout: float | None = None,
    ) -> bool:
        budget = timeout if timeout is not None else max(5.0, deadline - self._clock() + 30.0)
        return self._run_sync(
            self.wait_until(
                deadline,
                host=host,
                key=key,
                telemetry_ctx=telemetry_ctx,
                tick_interval=tick_interval,
                cancel_event=cancel_event,
            ),
            budget,
            {**dict(telemetry_ctx or {}), "host": host, "key": str(key)},
        )

    def wait_for_sync(
        self,
        host: str,
        key: str,
        *,
        telemetry_ctx: Mapping[str, Any] | None = None,
        tick_interval: float | None = None,
        cancel_event: Any = None,
        timeout: float | None = None,
    ) -> bool:
        state = self.armed_state(host, key)
        budget = timeout
        if budget is None and state is not None:
            budget = max(5.0, state.deadline - self._clock() + 30.0)
        completed = self._run_sync(
            self.wait_for(
                host,
                key,
                telemetry_ctx=telemetry_ctx,
                tick_interval=tick_interval,
                cancel_event=cancel_event,
            ),
            budget,
            {**dict(telemetry_ctx or {}), "host": host, "key": str(key)},
        )
        provider_wait.check_control()
        return completed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Stops the internal runtime loop (only needed for clean shutdowns)."""
        with self._runtime_lock:
            loop = self._runtime_loop
            thread = self._runtime_thread
            self._runtime_loop = None
            self._runtime_thread = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)


timer_scheduler = TimerScheduler()
