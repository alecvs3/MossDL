"""Adaptive admission spacing for provider-resolution requests."""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(slots=True)
class _HostState:
    delay_seconds: float
    clean_successes: int = 0
    rejections: int = 0


class ResolveStaggerController:
    """Increase spacing on explicit rejection and cautiously recover on success."""

    def __init__(self, *, initial: float = 1.5, floor: float = 0.5,
                 ceiling: float = 8.0, recovery_successes: int = 4) -> None:
        self.initial = initial
        self.floor = floor
        self.ceiling = ceiling
        self.recovery_successes = recovery_successes
        self._states: dict[str, _HostState] = {}
        self._lock = threading.Lock()

    def delay(self, host: str) -> float:
        clean = host.lower().strip()
        if clean in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
            return 0.0
        with self._lock:
            return self._states.setdefault(clean, _HostState(self.initial)).delay_seconds

    def observe_success(self, host: str) -> float:
        with self._lock:
            state = self._states.setdefault(host.lower(), _HostState(self.initial))
            state.clean_successes += 1
            if state.clean_successes >= self.recovery_successes:
                state.delay_seconds = max(self.floor, state.delay_seconds * 0.85)
                state.clean_successes = 0
            return state.delay_seconds

    def observe_rejection(self, host: str, *, status_code: int | None = None,
                          reason: str = "") -> float:
        """Apply feedback only for an attributable rate/challenge rejection."""
        normalized = reason.lower()
        is_rate = status_code in {429, 509} or "rate" in normalized or "wait" in normalized
        is_challenge_rejection = any(marker in normalized for marker in (
            "continuation_rejected", "provider_challenge_after_token", "solver_rejected",
        ))
        if not is_rate and not is_challenge_rejection:
            return self.delay(host)
        with self._lock:
            state = self._states.setdefault(host.lower(), _HostState(self.initial))
            multiplier = 1.75 if is_rate else 1.35
            state.delay_seconds = min(self.ceiling, max(self.initial, state.delay_seconds * multiplier))
            state.clean_successes = 0
            state.rejections += 1
            return state.delay_seconds

    def snapshot(self, host: str) -> dict[str, float | int]:
        with self._lock:
            state = self._states.setdefault(host.lower(), _HostState(self.initial))
            return {
                "delay_seconds": state.delay_seconds,
                "clean_successes": state.clean_successes,
                "rejections": state.rejections,
            }
