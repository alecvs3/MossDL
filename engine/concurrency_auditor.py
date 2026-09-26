"""Persistent Host Concurrency Auditor, Dynamic Ladder, and Host Pressure Breaker.

Monitors active streaming downloads across providers/hosts, dynamically probes
concurrency limits (e.g. 1 -> 2 -> 3), verifies active concurrent byte transfers
without false positives, detects stream preemptions/denials, and persists concise
machine-readable audit logs to disk.

The authoritative backpressure gate is ``admission_decision``. A per-host
``HostPressureBreaker`` runs CLOSED -> FROZEN -> PROBATION -> CLOSED: pressure
trips (429, throttling 403, Retry-After, Cloudflare idle timeouts, connection
aborts after a partial chunk) freeze the host at baseline 1 with an escalating
cooldown; after the cooldown a single jittered probe is admitted and must
sustain K successful chunks over >= 10 s (or 3 complete transfers) before the
ceiling is promoted one step. An explicit user retry clears the breaker via
``reset_breaker``.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, List, Optional
from urllib.parse import urlsplit

from .telemetry import telemetry_bus

logger = logging.getLogger(__name__)

# Default audit file path — resolved relative to this module so it is portable
DEFAULT_AUDIT_LOG = Path(__file__).parent.parent / "logs" / "host_concurrency_audit.jsonl"

BREAKER_CLOSED = "closed"
BREAKER_FROZEN = "frozen"
BREAKER_PROBATION = "probation"

_BREAKER_BASELINE = 1
_BREAKER_MIN_COOLDOWN = 300.0
_BREAKER_MAX_COOLDOWN = 900.0
_BREAKER_K_SUCCESSES = 5
_BREAKER_SUCCESS_SPAN_SECONDS = 10.0
_BREAKER_RANGE_COMPLETIONS = 3
_BREAKER_DECAY_SECONDS = 24 * 60 * 60.0
_BREAKER_DECAY_CAP = 8
_BREAKER_MAX_TRIP_STEPS = 3
_BREAKER_STATE_PREFIX = "__breaker__:"
# Upward probing on a CLOSED breaker: one extra stream at a time, at most this
# often, never beyond this cap.
_OPTIMISTIC_PROBE_INTERVAL = 90.0
_OPTIMISTIC_PROBE_CAP = 8
# How long a starvation incident suppresses upward probing on that host.
_STARVATION_LOCK_SECONDS = 600.0
# A stream idle this long while a sibling runs is not ordinary bandwidth sharing.
# The old 6s threshold fired constantly at high single-stream throughput.
_PREEMPTION_IDLE_SECONDS = 45.0

_THROTTLE_MARKERS = (
    "too many", "rate limit", "rate-limit", "ratelimit", "throttl", "concurr",
    "slow down", "retry later", "limit exceeded", "quota exceeded",
)
_HTML_MARKERS = (
    "<html", "<!doctype html", "cloudflare", "attention required", "just a moment", "turnstile",
)
_RESET_MARKERS = (
    "connection reset", "connection aborted", "connectionreseterror", "reset by peer",
    "incomplete read", "read aborted", "read timed out", "remotedisconnected", "econnreset",
    "remote end closed connection",
)
_CLOUDFLARE_IDLE_CODES = ("520", "522", "524")


def is_storage_host(host: str) -> bool:
    """Return True if host is a storage node domain (e.g. *.datanodes.to, dlproxy.uk)."""
    if not host:
        return False
    clean = host.lower().strip()
    if clean == "datanodes.to" or clean.endswith(".datanodes.to"):
        return True
    if clean == "dlproxy.uk" or clean.endswith(".dlproxy.uk"):
        return True
    parts = clean.split(".")
    if len(parts) >= 3 and parts[0].startswith(("node", "tunnel", "storage", "cdn", "srv", "edge")):
        return True
    return False


def classify_host_pressure(
    *,
    status_code: Optional[int] = None,
    retry_after: float = 0.0,
    response_text: str = "",
    error_text: str = "",
    partial_chunks: int = 0,
) -> Optional[str]:
    """Classify a transfer failure as confirmed host pressure, or return None.

    Confirmed signals: any Retry-After, HTTP 429, HTTP 403 carrying throttle or
    HTML/challenge markers, a Cloudflare idle timeout, or a connection reset /
    read abort that happened after at least one partial chunk.
    """
    text = f"{response_text}\n{error_text}".lower()
    if retry_after and float(retry_after) > 0:
        return "retry_after"
    if status_code == 429:
        return "http_429"
    if status_code == 403:
        if any(marker in text for marker in _THROTTLE_MARKERS + _HTML_MARKERS):
            return "http_403_throttle"
        return None
    if "idle timeout" in text or ("cloudflare" in text and any(code in text for code in _CLOUDFLARE_IDLE_CODES)):
        return "cloudflare_idle_timeout"
    if partial_chunks >= 1 and any(marker in text for marker in _RESET_MARKERS):
        return "connection_abort_with_partial_chunk"
    return None


@dataclass
class HostPressureBreaker:
    """One authoritative breaker per host, persisted with the host profile."""

    state: str = BREAKER_CLOSED
    baseline: int = _BREAKER_BASELINE
    consecutive_trips: int = 0
    cooldown_until: float = 0.0
    last_trip_at: float = 0.0
    last_reason: str = ""
    probe_task_id: str = ""
    probe_target: int = 0
    probe_started_at: float = 0.0
    probe_chunk_successes: int = 0
    probe_first_success_at: float = 0.0
    probe_last_success_at: float = 0.0
    probe_range_completions: int = 0
    probe_jitter_seconds: float = 0.0
    pending_close_telemetry: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "baseline": int(self.baseline),
            "consecutive_trips": int(self.consecutive_trips),
            "cooldown_until": float(self.cooldown_until),
            "last_trip_at": float(self.last_trip_at),
            "last_reason": self.last_reason,
            "probe_task_id": self.probe_task_id,
            "probe_target": int(self.probe_target),
            "probe_started_at": float(self.probe_started_at),
            "probe_chunk_successes": int(self.probe_chunk_successes),
            "probe_first_success_at": float(self.probe_first_success_at),
            "probe_last_success_at": float(self.probe_last_success_at),
            "probe_range_completions": int(self.probe_range_completions),
            "probe_jitter_seconds": float(self.probe_jitter_seconds),
            "pending_close_telemetry": bool(self.pending_close_telemetry),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "HostPressureBreaker":
        breaker = cls()
        if not isinstance(payload, dict):
            return breaker
        for name, default in breaker.to_payload().items():
            if name not in payload:
                continue
            value = payload[name]
            if isinstance(default, bool):
                setattr(breaker, name, bool(value))
            elif isinstance(default, int):
                try:
                    setattr(breaker, name, int(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(default, float):
                try:
                    setattr(breaker, name, float(value))
                except (TypeError, ValueError):
                    pass
            else:
                setattr(breaker, name, str(value))
        if breaker.state not in {BREAKER_CLOSED, BREAKER_FROZEN, BREAKER_PROBATION}:
            breaker.state = BREAKER_CLOSED
        breaker.baseline = max(1, breaker.baseline)
        return breaker


def _split_breaker_marker(reasons: list[Any]) -> tuple[list[str], dict[str, Any]]:
    """Split persisted rejection reasons into clean reasons and the breaker payload."""
    clean: list[str] = []
    payload: dict[str, Any] = {}
    for reason in reasons or []:
        text = str(reason)
        if text.startswith(_BREAKER_STATE_PREFIX):
            try:
                data = json.loads(text[len(_BREAKER_STATE_PREFIX):])
                if isinstance(data, dict):
                    payload = data
            except Exception as err:
                logger.warning("discarding corrupt persisted breaker state: %s", err)
                continue
        else:
            clean.append(text)
    return clean, payload


class CooldownStatus(tuple):
    """Tuple of (in_cooldown, remaining_seconds, reason) with boolean evaluation."""

    def __new__(cls, in_cooldown: bool, remaining_seconds: float, reason: str):
        return super().__new__(cls, (in_cooldown, remaining_seconds, reason))

    def __bool__(self) -> bool:
        return bool(self[0])

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, bool):
            return bool(self[0]) == other
        return super().__eq__(other)


@dataclass
class StreamState:
    task_id: str
    host: str
    started_at: float
    last_active_at: float
    bytes_transferred: int = 0
    speed_bps: float = 0.0
    verified_active: bool = False
    attempted_concurrency: int | None = None
    status: str = "active"  # active, completed, denied, preempted


@dataclass
class HostConcurrencyProfile:
    host: str
    verified_ceiling: int = 1
    current_in_flight: int = 0
    denied_at_ceiling: Optional[int] = None
    last_probe_at: float = 0.0
    cooldown_until: float = 0.0
    streams: dict[str, StreamState] = field(default_factory=dict)
    pending_probe_targets: dict[str, int] = field(default_factory=dict)
    rejection_reasons: list[str] = field(default_factory=list)
    breaker: HostPressureBreaker = field(default_factory=HostPressureBreaker)
    ceiling_set_at: float = 0.0
    ceiling_locked: bool = False


class HostConcurrencyAuditor:
    """Singleton auditor tracking active streams and writing conclusive audit entries to disk."""

    def __init__(
        self,
        log_path: Optional[Path] = None,
        store: Optional[Any] = None,
        profiles_path: Optional[Path] = None,
    ) -> None:
        # Until an engine attaches its data folder, nothing is written here:
        # the program folder is read-only once installed.
        self.log_path = log_path or DEFAULT_AUDIT_LOG
        self.profiles_path = profiles_path or (self.log_path.parent / "host_concurrency_profiles.json")
        self.store = store
        self._lock = threading.RLock()
        self._profiles: dict[str, HostConcurrencyProfile] = {}
        self._watchdog_thread: Optional[threading.Thread] = None
        self._running = True
        # Profiles are loaded by attach_store() once the DB is live.
        # If a store was passed directly (e.g. in tests), load immediately.
        if store is not None:
            self.load_persisted_profiles()

    def _append_audit(self, event_type: str, host: str, data: dict[str, Any]) -> None:
        """Atomically append a structured JSON line with microsecond timestamp to disk."""
        now_utc = datetime.now(timezone.utc).isoformat()
        entry = {
            "timestamp": now_utc,
            "perf_ts": round(time.perf_counter(), 6),
            "host": host,
            "event": event_type,
            **data,
        }
        try:
            line = json.dumps(entry) + "\n"
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as exc:
            logger.warning("Failed to write concurrency audit log: %s", exc)

        # Telemetry bus mirror
        mirror_level = "WARN" if any(tag in event_type for tag in ("DENIED", "TRIPPED", "STALL", "PREEMPTION")) else "INFO"
        telemetry_bus.record(
            level=mirror_level,
            subsystem="engine:concurrency",
            message=f"[CONCURRENCY_AUDIT] [{host}] {event_type}: {data.get('summary', '')}",
            context={"host": host, "event": event_type, **data},
            tier="engine",
        )

    def attach_store(self, store: Any, log_dir: Optional[Path] = None) -> None:
        """Use this engine's database and data folder for learned host limits.

        Limits learned against one data folder (a benchmark, a test run, another
        install) must not throttle another: they used to share a JSON file in the
        program folder, so a strict test host capped every later run at one file.
        """
        self.store = store
        if log_dir is not None:
            with self._lock:
                self._profiles.clear()
            self.log_path = Path(log_dir) / "host_concurrency_audit.jsonl"
            self.profiles_path = Path(log_dir) / "host_concurrency_profiles.json"
        self.load_persisted_profiles()

    def is_host_in_cooldown(self, host: str) -> CooldownStatus:
        """Return (True, remaining_seconds, reason) while any host cooldown or breaker freeze is active."""
        clean = host.lower().strip()
        now = time.time()
        with self._lock:
            prof = self._profiles.get(clean)
            if not prof:
                return CooldownStatus(False, 0.0, "")
            deadline = prof.cooldown_until
            if prof.breaker.state == BREAKER_FROZEN:
                deadline = max(deadline, prof.breaker.cooldown_until)
            if deadline > now:
                remaining = deadline - now
                if prof.breaker.state == BREAKER_FROZEN:
                    reason = (
                        f"Host pressure breaker frozen ({prof.breaker.last_reason or 'pressure'}; "
                        f"{int(remaining)}s remaining) - limit clamped at {prof.breaker.baseline}"
                    )
                else:
                    reason = (
                        f"Host cooldown active ({int(remaining)}s remaining) - "
                        f"limit clamped at {prof.verified_ceiling}"
                    )
                return CooldownStatus(True, remaining, reason)
            return CooldownStatus(False, 0.0, "")

    def _save_profiles_json(self) -> None:
        if not self.profiles_path:
            return
        data = {}
        with self._lock:
            for h, p in self._profiles.items():
                clean_reasons = [
                    r for r in p.rejection_reasons if not str(r).startswith(_BREAKER_STATE_PREFIX)
                ]
                data[h] = {
                    "verified_ceiling": p.verified_ceiling,
                    "denied_at_ceiling": p.denied_at_ceiling,
                    "cooldown_until": p.cooldown_until,
                    "last_probe_at": p.last_probe_at,
                    "rejection_reasons": clean_reasons,
                    "ceiling_set_at": p.ceiling_set_at,
                    "ceiling_locked": p.ceiling_locked,
                    "breaker": p.breaker.to_payload(),
                }
        try:
            self.profiles_path.parent.mkdir(parents=True, exist_ok=True)
            self.profiles_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to write profiles JSON: %s", exc)

    def save_profile(self, host: str) -> None:
        clean = host.lower().strip()
        with self._lock:
            prof = self._profiles.get(clean)
            if not prof:
                return
            verified = prof.verified_ceiling
            denied = prof.denied_at_ceiling
            cooldown = prof.cooldown_until
            last_probe = prof.last_probe_at
            reasons = [
                r for r in prof.rejection_reasons if not str(r).startswith(_BREAKER_STATE_PREFIX)
            ]
            marker = _BREAKER_STATE_PREFIX + json.dumps(
                {
                    "ceiling_set_at": prof.ceiling_set_at,
                    "ceiling_locked": prof.ceiling_locked,
                    **prof.breaker.to_payload(),
                },
                sort_keys=True,
            )
            reasons.append(marker)

        if self.store is not None:
            try:
                self.store.save_host_concurrency_profile(
                    host=clean,
                    verified_ceiling=verified,
                    denied_at_ceiling=denied,
                    cooldown_until=cooldown,
                    last_probe_at=last_probe,
                    rejection_reasons=reasons,
                )
            except Exception as exc:
                logger.warning("Failed to persist host profile to TaskStore: %s", exc)
                telemetry_bus.record(
                    level="WARN", subsystem="engine:concurrency",
                    message=f"[PROFILE_SAVE_FAILED] Could not persist concurrency profile for {clean}: {exc}",
                    context={"host": clean, "error": str(exc)},
                    tier="engine",
                )
        self._save_profiles_json()

    def load_persisted_profiles(self) -> None:
        now = time.time()
        loaded = False
        if self.store is not None:
            try:
                rows = self.store.list_host_concurrency_profiles()
                if rows:
                    with self._lock:
                        for r in rows:
                            h = str(r["host"]).lower().strip()
                            prof = self._profiles.setdefault(h, HostConcurrencyProfile(host=h))
                            prof.verified_ceiling = int(r.get("verified_ceiling") or 1)
                            prof.denied_at_ceiling = r.get("denied_at_ceiling")
                            cd = float(r.get("cooldown_until") or 0.0)
                            prof.cooldown_until = cd if cd > now else 0.0
                            prof.last_probe_at = float(r.get("last_probe_at") or 0.0)
                            reasons, payload = _split_breaker_marker(r.get("rejection_reasons"))
                            prof.rejection_reasons = reasons
                            prof.breaker = HostPressureBreaker.from_payload(payload)
                            prof.ceiling_set_at = float(
                                payload.get("ceiling_set_at") or r.get("updated_at") or now
                            )
                            prof.ceiling_locked = bool(payload.get("ceiling_locked") or False)
                            if prof.breaker.cooldown_until > now and prof.breaker.state == BREAKER_FROZEN:
                                prof.cooldown_until = max(prof.cooldown_until, prof.breaker.cooldown_until)
                    loaded = True
            except Exception as exc:
                logger.warning("Failed to load profiles from TaskStore: %s", exc)

        if not loaded and self.profiles_path and self.profiles_path.exists():
            try:
                raw = json.loads(self.profiles_path.read_text(encoding="utf-8") or "{}")
                with self._lock:
                    for h, pdata in raw.items():
                        clean = str(h).lower().strip()
                        prof = self._profiles.setdefault(clean, HostConcurrencyProfile(host=clean))
                        prof.verified_ceiling = int(pdata.get("verified_ceiling") or 1)
                        prof.denied_at_ceiling = pdata.get("denied_at_ceiling")
                        cd = float(pdata.get("cooldown_until") or 0.0)
                        prof.cooldown_until = cd if cd > now else 0.0
                        prof.last_probe_at = float(pdata.get("last_probe_at") or 0.0)
                        prof.rejection_reasons = list(pdata.get("rejection_reasons") or [])
                        prof.breaker = HostPressureBreaker.from_payload(pdata.get("breaker"))
                        prof.ceiling_set_at = float(pdata.get("ceiling_set_at") or 0.0)
                        prof.ceiling_locked = bool(pdata.get("ceiling_locked") or False)
                        if prof.breaker.cooldown_until > now and prof.breaker.state == BREAKER_FROZEN:
                            prof.cooldown_until = max(prof.cooldown_until, prof.breaker.cooldown_until)
                loaded = True
            except Exception as exc:
                logger.warning("Failed to load profiles from JSON: %s", exc)

        with self._lock:
            count = len(self._profiles)
        source = "sqlite" if (self.store is not None and loaded) else ("json" if loaded else "none")
        telemetry_bus.record(
            level="INFO", subsystem="engine:concurrency",
            message=f"Loaded persisted host concurrency profiles | source={source} count={count}",
            context={"profile_count": count, "source": source},
            tier="engine",
        )

    def get_profile(self, host: str) -> HostConcurrencyProfile:
        clean = host.lower().strip()
        with self._lock:
            if clean not in self._profiles:
                self._profiles[clean] = HostConcurrencyProfile(host=clean)
            return self._profiles[clean]

    def list_profiles(self) -> list[HostConcurrencyProfile]:
        with self._lock:
            return list(self._profiles.values())

    def admission_decision(
        self,
        host: str,
        task_id: str | None = None,
        active: int | None = None,
        max_global: int = 100,
    ) -> tuple[bool, int, str]:
        """Authoritative admission gate: (allowed, target_concurrency, reason).

        Honors the per-host pressure breaker before the optimistic ladder. The
        caller must treat ``allowed`` as authoritative rather than optimistic.
        """
        clean = host.lower().strip()
        now = time.time()
        with self._lock:
            prof = self._profiles.setdefault(clean, HostConcurrencyProfile(host=clean))
            self._decay_ceiling_locked(prof, now)
            breaker = prof.breaker

            if breaker.state == BREAKER_CLOSED and breaker.pending_close_telemetry:
                breaker.pending_close_telemetry = False
                self._append_audit("BREAKER_CLOSED", clean, {
                    "verified_ceiling": prof.verified_ceiling,
                    "summary": (
                        f"[BREAKER_CLOSED] {clean} fully closed; verified ceiling "
                        f"{prof.verified_ceiling}, ladder reopened"
                    ),
                })

            active_count = (
                max(0, int(active))
                if active is not None
                else len([s for s in prof.streams.values() if s.status == "active"])
            )

            if breaker.state == BREAKER_FROZEN:
                deadline = max(breaker.cooldown_until, prof.cooldown_until)
                if now < deadline:
                    return (
                        False,
                        max(1, breaker.baseline),
                        (
                            f"Host pressure breaker frozen ({breaker.last_reason or 'pressure'}; "
                            f"{int(deadline - now)}s remaining)"
                        ),
                    )
                self._expire_frozen_cooldown_locked(prof, clean, now, task_id or "")

            if breaker.state == BREAKER_PROBATION:
                probe_cap = max(1, min(int(max_global), breaker.probe_target or breaker.baseline + 1))
                if active_count >= probe_cap:
                    return (
                        False,
                        probe_cap,
                        (
                            f"Host pressure probation probe already in flight "
                            f"({active_count}/{probe_cap}); awaiting "
                            f"{breaker.probe_chunk_successes}/{_BREAKER_K_SUCCESSES} sustained successes"
                        ),
                    )
                target = min(active_count + 1, probe_cap)
                if target >= probe_cap and task_id:
                    breaker.probe_task_id = task_id
                return (
                    True,
                    target,
                    (
                        f"Host pressure probation probe {target}/{probe_cap}; "
                        f"{breaker.probe_chunk_successes}/{_BREAKER_K_SUCCESSES} sustained successes"
                    ),
                )

            # If in cooldown after a denial
            if prof.cooldown_until > now:
                remain = int(prof.cooldown_until - now)
                return False, prof.verified_ceiling, f"Host cooldown active ({remain}s remaining) - limit clamped at {prof.verified_ceiling}"

            # A denial is a temporary calibration boundary, not a permanent
            # ceiling. Reopen exactly one controlled probe ladder once the
            # cooldown has elapsed; a fresh denial establishes a new boundary.
            if not prof.ceiling_locked and prof.denied_at_ceiling is not None:
                previous_denial = prof.denied_at_ceiling
                prof.denied_at_ceiling = None
                prof.last_probe_at = now
                self._append_audit("PROBE_REOPENED", clean, {
                    "previous_denied_at_ceiling": previous_denial,
                    "verified_ceiling": prof.verified_ceiling,
                    "summary": f"Calibration cooldown expired; reopening probe ladder above {prof.verified_ceiling}",
                })
                self.save_profile(clean)

            # If below verified ceiling -> admit normally
            if active_count < prof.verified_ceiling:
                return True, active_count + 1, f"Admitted within verified ceiling ({active_count + 1}/{prof.verified_ceiling})"

            if prof.ceiling_locked:
                return (
                    False,
                    prof.verified_ceiling,
                    f"Host concurrency ceiling locked at {prof.verified_ceiling} (probes suppressed due to starvation incident)",
                )

            # Optimistic ladder: admit the next queued stream until max_global.
            next_count = active_count + 1
            if next_count <= max_global:
                if prof.denied_at_ceiling is None or next_count < prof.denied_at_ceiling:
                    prof.last_probe_at = now
                    if task_id:
                        prof.pending_probe_targets[task_id] = next_count
                    self._append_audit("PROBE_ATTEMPT", clean, {
                        "attempted_concurrency": next_count,
                        "current_verified": prof.verified_ceiling,
                        "summary": f"Probing concurrency step-up to {next_count} streams",
                    })
                    return True, next_count, f"Probing dynamic concurrency step-up to {next_count}"

            return (False, prof.verified_ceiling, f"Host at maximum concurrency ceiling ({prof.verified_ceiling})")

    def _expire_frozen_cooldown_locked(
        self, prof: HostConcurrencyProfile, clean: str, now: float, task_id: str = ""
    ) -> bool:
        """Reopen a frozen breaker whose cooldown has elapsed. Caller holds ``_lock``.

        This must be reachable from EVERY breaker consultation, not just
        ``admission_decision``: that gate is only called for tasks resolving more
        than one item, so a host frozen during an ordinary single-item download
        had no path back to PROBATION and stayed clamped at a ceiling of 1 for the
        life of the process. ``_decay_ceiling_locked`` cannot recover it either --
        it deliberately only relaxes a CLOSED breaker.
        """
        breaker = prof.breaker
        if breaker.state != BREAKER_FROZEN:
            return False
        if now < max(breaker.cooldown_until, prof.cooldown_until):
            return False
        prof.cooldown_until = 0.0
        prof.verified_ceiling = min(prof.verified_ceiling, breaker.baseline)
        prof.ceiling_set_at = now
        breaker.state = BREAKER_PROBATION
        breaker.probe_task_id = ""
        breaker.probe_target = max(1, breaker.baseline + 1)
        breaker.probe_started_at = now
        breaker.probe_chunk_successes = 0
        breaker.probe_first_success_at = 0.0
        breaker.probe_last_success_at = 0.0
        breaker.probe_range_completions = 0
        breaker.probe_jitter_seconds = random.uniform(0.0, 2.5)
        self._append_audit("BREAKER_PROBING", clean, {
            "probe_target": breaker.probe_target,
            "task_id": task_id,
            "jitter_seconds": round(breaker.probe_jitter_seconds, 3),
            "summary": (
                f"[BREAKER_PROBING] {clean} cooldown elapsed; admitting one jittered "
                f"probe stream toward {breaker.probe_target}"
            ),
        })
        self.save_profile(clean)
        return True

    def probe_allowance(self, host: str, current_limit: int, demand: int = 0) -> int:
        """Streams permitted right now for a healthy host, including one probe.

        `verified_ceiling` is only ever RAISED by observing more active streams
        than the ceiling (see the ironclad rule in `_note_stream_progress`) --
        but the ceiling is exactly what prevents those streams from starting. A
        host calibrated to 1 therefore stayed at 1 forever regardless of how much
        work queued behind it. When real demand is waiting and the host looks
        healthy, admit one extra stream and let the ratchet decide.

        Deliberately conservative: one stream at a time, never while the breaker
        is doing its own laddering, never above a level the host already refused,
        and never more often than `_OPTIMISTIC_PROBE_INTERVAL`.
        """
        clean = host.lower().strip()
        now = time.time()
        with self._lock:
            prof = self._profiles.get(clean)
            if prof is None or demand <= 0:
                return current_limit
            breaker = prof.breaker
            if breaker.state != BREAKER_CLOSED:
                # FROZEN/PROBATION own the ladder; do not race them.
                return current_limit
            if now < prof.cooldown_until:
                return current_limit
            if prof.ceiling_locked:
                # Reaching here means the starvation cooldown above has already
                # elapsed, so the lock has served its purpose. Clear it and fall
                # through to normal probing rather than burning another cycle.
                prof.ceiling_locked = False
            target = max(int(current_limit), int(prof.verified_ceiling))
            if target >= _OPTIMISTIC_PROBE_CAP:
                return current_limit
            # A denial is a TEMPORARY calibration boundary, not a permanent
            # ceiling -- the same contract `admission_decision` documents. That
            # expiry only existed there, and that function is gated behind
            # `item_concurrency > 1`, so it never runs for single-item package
            # parts: denials were set and never cleared. Combined with the guard
            # below, a host that once refused a second stream was pinned at one
            # stream forever, which is exactly the "multiple parts stopped
            # working again" regression.
            if prof.denied_at_ceiling is not None:
                previous_denial = int(prof.denied_at_ceiling)
                prof.denied_at_ceiling = None
                self._append_audit("PROBE_REOPENED", clean, {
                    "previous_denied_at_ceiling": previous_denial,
                    "verified_ceiling": prof.verified_ceiling,
                    "summary": (
                        f"[PROBE_REOPENED] {clean} calibration cooldown elapsed; retiring the "
                        f"denial boundary at {previous_denial} and reopening the ladder"
                    ),
                })
            if now - prof.last_probe_at < _OPTIMISTIC_PROBE_INTERVAL:
                return current_limit
            prof.last_probe_at = now
            allowance = target + 1
            self._append_audit("CEILING_PROBE_OPENED", clean, {
                "previous_ceiling": prof.verified_ceiling,
                "probe_allowance": allowance,
                "demand": int(demand),
                "summary": (
                    f"[CEILING_PROBE_OPENED] {clean} healthy with {demand} task(s) waiting; "
                    f"admitting one probe stream toward {allowance}"
                ),
            })
            return allowance

    def breaker_admission_limit(self, host: str) -> Optional[int]:
        """Return the breaker-enforced host limit, or None when the breaker is closed."""
        clean = host.lower().strip()
        with self._lock:
            prof = self._profiles.get(clean)
            if not prof:
                return None
            self._expire_frozen_cooldown_locked(prof, clean, time.time())
            breaker = prof.breaker
            if breaker.state == BREAKER_FROZEN:
                return max(1, breaker.baseline)
            if breaker.state == BREAKER_PROBATION:
                return max(1, breaker.probe_target or breaker.baseline + 1)
            return None

    def breaker_snapshot(self, host: str) -> dict[str, Any]:
        """Structured breaker/window state for telemetry consumers."""
        clean = host.lower().strip()
        now = time.time()
        with self._lock:
            prof = self._profiles.get(clean)
            if not prof:
                return {"host": clean, "state": BREAKER_CLOSED, "known": False}
            breaker = prof.breaker
            return {
                "host": clean,
                "known": True,
                "state": breaker.state,
                "baseline": breaker.baseline,
                "consecutive_trips": breaker.consecutive_trips,
                "reason": breaker.last_reason,
                "cooldown_remaining": max(0.0, round(breaker.cooldown_until - now, 3)),
                "probe_task_id": breaker.probe_task_id,
                "probe_target": breaker.probe_target,
                "probe_chunk_successes": breaker.probe_chunk_successes,
                "probe_range_completions": breaker.probe_range_completions,
                "verified_ceiling": prof.verified_ceiling,
                "ceiling_set_at": prof.ceiling_set_at,
            }

    def _decay_ceiling_locked(self, prof: HostConcurrencyProfile, now: float) -> None:
        """Relax a clean, closed ceiling by +1 per 24 h without pressure signals."""
        if prof.ceiling_set_at <= 0.0:
            prof.ceiling_set_at = now
            return
        if prof.ceiling_locked or prof.breaker.state != BREAKER_CLOSED or prof.cooldown_until > now:
            return
        elapsed = now - prof.ceiling_set_at
        if elapsed < _BREAKER_DECAY_SECONDS:
            return
        steps = int(elapsed // _BREAKER_DECAY_SECONDS)
        cap = max(_BREAKER_DECAY_CAP, prof.verified_ceiling)
        new_ceiling = min(cap, prof.verified_ceiling + steps)
        prof.ceiling_set_at += steps * _BREAKER_DECAY_SECONDS
        if new_ceiling != prof.verified_ceiling:
            previous = prof.verified_ceiling
            prof.verified_ceiling = new_ceiling
            self._append_audit("CEILING_DECAYED", prof.host, {
                "previous_ceiling": previous,
                "new_ceiling": new_ceiling,
                "clean_hours": round(elapsed / 3600.0, 1),
                "summary": (
                    f"Clean for {int(elapsed // 3600)}h; decaying verified ceiling "
                    f"{previous} -> {new_ceiling}"
                ),
            })
            self.save_profile(prof.host)

    def _trip_breaker_locked(
        self,
        prof: HostConcurrencyProfile,
        reason: str,
        *,
        status_code: Optional[int],
        retry_after: float,
        attempted: Optional[int],
        task_id: str,
        response_text: str = "",
    ) -> float:
        """Freeze the host breaker; must be called with ``self._lock`` held."""
        breaker = prof.breaker
        now = time.time()
        if breaker.state == BREAKER_CLOSED:
            breaker.consecutive_trips = 1
        else:
            breaker.consecutive_trips = min(
                max(1, breaker.consecutive_trips) + 1, _BREAKER_MAX_TRIP_STEPS
            )
        base = max(float(retry_after or 0.0), _BREAKER_MIN_COOLDOWN)
        cooldown = min(base * (2 ** (breaker.consecutive_trips - 1)), _BREAKER_MAX_COOLDOWN)
        breaker.state = BREAKER_FROZEN
        breaker.baseline = _BREAKER_BASELINE
        breaker.cooldown_until = now + cooldown
        breaker.last_trip_at = now
        breaker.last_reason = reason
        breaker.probe_task_id = ""
        breaker.probe_target = 0
        breaker.probe_started_at = 0.0
        breaker.probe_chunk_successes = 0
        breaker.probe_first_success_at = 0.0
        breaker.probe_last_success_at = 0.0
        breaker.probe_range_completions = 0
        breaker.probe_jitter_seconds = 0.0
        breaker.pending_close_telemetry = False
        if prof.verified_ceiling > breaker.baseline:
            prof.verified_ceiling = breaker.baseline
        prof.ceiling_set_at = now
        snippet = response_text.replace("\n", " ").strip()[:180]
        prof.rejection_reasons.append(f"{reason} (status={status_code}, cooldown={int(cooldown)}s)")
        if len(prof.rejection_reasons) > 32:
            del prof.rejection_reasons[:-32]
        self._append_audit("BREAKER_TRIPPED", prof.host, {
            "reason": reason,
            "http_status": status_code,
            "retry_after_seconds": retry_after,
            "attempted_concurrency": attempted,
            "consecutive_trips": breaker.consecutive_trips,
            "cooldown_seconds": round(cooldown, 2),
            "baseline": breaker.baseline,
            "task_id": task_id,
            "error_snippet": snippet,
            "summary": (
                f"[BREAKER_TRIPPED] {prof.host} frozen for {int(cooldown)}s "
                f"(reason={reason}, trips={breaker.consecutive_trips}, baseline={breaker.baseline})"
            ),
        })
        return cooldown

    def note_pressure_failure(
        self,
        host: str,
        task_id: str = "",
        *,
        status_code: Optional[int] = None,
        retry_after: float = 0.0,
        response_text: str = "",
        error_text: str = "",
        partial_chunks: int = 0,
    ) -> Optional[str]:
        """Classify a failure and trip the authoritative pressure breaker when confirmed."""
        reason = classify_host_pressure(
            status_code=status_code,
            retry_after=retry_after,
            response_text=response_text,
            error_text=error_text,
            partial_chunks=partial_chunks,
        )
        if reason is None:
            return None
        clean = host.lower().strip()
        with self._lock:
            prof = self._profiles.setdefault(clean, HostConcurrencyProfile(host=clean))
            stream = prof.streams.get(task_id)
            if stream is None and task_id:
                matching = [
                    candidate for key, candidate in prof.streams.items()
                    if key.startswith(f"{task_id}:") and candidate.status == "active"
                ]
                stream = matching[0] if matching else None
            attempted = stream.attempted_concurrency if stream else None
            if attempted is None:
                attempted = len([s for s in prof.streams.values() if s.status == "active"]) + 1
            if stream is not None:
                stream.status = "denied"
            prof.denied_at_ceiling = attempted
            self._trip_breaker_locked(
                prof,
                reason,
                status_code=status_code,
                retry_after=retry_after,
                attempted=attempted,
                task_id=task_id,
                response_text=response_text or error_text,
            )
        self.save_profile(clean)
        return reason

    def _note_probe_success_locked(
        self,
        prof: HostConcurrencyProfile,
        task_id: str,
        *,
        bytes_delta: int,
        completed_range: bool,
        at: float,
    ) -> bool:
        """Track probation probe progress; must be called with ``self._lock`` held."""
        breaker = prof.breaker
        if breaker.state != BREAKER_PROBATION:
            return False
        probe_root = breaker.probe_task_id.split(":", 1)[0] if breaker.probe_task_id else ""
        task_root = task_id.split(":", 1)[0]
        if probe_root and task_root != probe_root:
            # The latched probe task can finish before it reaches the sustained
            # threshold (a small file completes in one range). Holding the latch
            # for a task that no longer has an active stream would strand the
            # breaker in PROBATION permanently: every later transfer on the host
            # would be rejected here, so nothing could ever close it. Hand the
            # probe to the reporting task instead.
            if any(
                stream.status == "active" and key.split(":", 1)[0] == probe_root
                for key, stream in prof.streams.items()
            ):
                return False
            self._append_audit("BREAKER_PROBE_REASSIGNED", prof.host, {
                "previous_probe_task_id": breaker.probe_task_id,
                "task_id": task_id,
                "summary": (
                    f"[BREAKER_PROBE_REASSIGNED] {prof.host} probe task "
                    f"{breaker.probe_task_id} ended before closing the breaker; "
                    f"reassigning the probe to {task_id}"
                ),
            })
            # The success counters are NOT reset: they are the evidence gathered
            # during this probation episode, not per-task state. Resetting them on
            # every hand-off makes the thresholds unreachable whenever tasks are
            # small enough to finish in a single range -- the common case. A
            # failure during probation re-trips the breaker on its own path.
            breaker.probe_task_id = task_id
        if not breaker.probe_task_id:
            breaker.probe_task_id = task_id
        if bytes_delta > 0:
            if breaker.probe_chunk_successes == 0:
                breaker.probe_first_success_at = at
            breaker.probe_last_success_at = at
            breaker.probe_chunk_successes += 1
        if completed_range:
            breaker.probe_range_completions += 1

        span = breaker.probe_last_success_at - breaker.probe_first_success_at
        sustained = (
            breaker.probe_chunk_successes >= _BREAKER_K_SUCCESSES
            and span >= _BREAKER_SUCCESS_SPAN_SECONDS + breaker.probe_jitter_seconds
        )
        completed = breaker.probe_range_completions >= _BREAKER_RANGE_COMPLETIONS
        if not (sustained or completed):
            return False

        previous = prof.verified_ceiling
        sustained_chunks = breaker.probe_chunk_successes
        range_completions = breaker.probe_range_completions
        prof.verified_ceiling = max(prof.verified_ceiling, breaker.probe_target or breaker.baseline + 1)
        prof.ceiling_set_at = at
        breaker.state = BREAKER_CLOSED
        breaker.consecutive_trips = 0
        breaker.cooldown_until = 0.0
        breaker.pending_close_telemetry = True
        breaker.probe_task_id = ""
        breaker.probe_target = 0
        breaker.probe_chunk_successes = 0
        breaker.probe_first_success_at = 0.0
        breaker.probe_last_success_at = 0.0
        breaker.probe_range_completions = 0
        prof.cooldown_until = 0.0
        self._append_audit("BREAKER_RECOVERED", prof.host, {
            "task_id": task_id,
            "previous_ceiling": previous,
            "new_ceiling": prof.verified_ceiling,
            "sustained_chunks": sustained_chunks,
            "span_seconds": round(span, 2),
            "range_completions": range_completions,
            "summary": (
                f"[BREAKER_RECOVERED] {prof.host} probe sustained "
                f"{sustained_chunks} chunks over {span:.1f}s; ceiling "
                f"{previous} -> {prof.verified_ceiling}"
            ),
        })
        return True

    def reset_breaker(self, host: str, reason: str = "manual_reset") -> bool:
        """Administratively close a breaker; returns True when a non-closed state was cleared."""
        clean = host.lower().strip()
        with self._lock:
            prof = self._profiles.setdefault(clean, HostConcurrencyProfile(host=clean))
            breaker = prof.breaker
            was_open = breaker.state != BREAKER_CLOSED or breaker.cooldown_until > 0 or prof.ceiling_locked
            prof.ceiling_locked = False
            breaker.state = BREAKER_CLOSED
            breaker.consecutive_trips = 0
            breaker.cooldown_until = 0.0
            breaker.last_reason = ""
            breaker.probe_task_id = ""
            breaker.probe_target = 0
            breaker.probe_chunk_successes = 0
            breaker.probe_first_success_at = 0.0
            breaker.probe_last_success_at = 0.0
            breaker.probe_range_completions = 0
            breaker.pending_close_telemetry = False
            prof.cooldown_until = 0.0
            if was_open:
                self._append_audit("BREAKER_CLOSED", clean, {
                    "reason": reason,
                    "verified_ceiling": prof.verified_ceiling,
                    "summary": f"[BREAKER_CLOSED] {clean} breaker closed via {reason}",
                })
        if was_open:
            self.save_profile(clean)
        return was_open

    def record_stream_started(self, host: str, task_id: str) -> None:
        clean = host.lower().strip()
        now = time.time()
        with self._lock:
            prof = self._profiles.setdefault(clean, HostConcurrencyProfile(host=clean))
            task_root = task_id.split(":", 1)[0]
            attempted = prof.pending_probe_targets.pop(task_root, None)
            prof.streams[task_id] = StreamState(
                task_id=task_id,
                host=clean,
                started_at=now,
                last_active_at=now,
                attempted_concurrency=attempted,
            )
            active_ids = [s.task_id for s in prof.streams.values() if s.status == "active"]
            self._append_audit("STREAM_STARTED", clean, {
                "task_id": task_id,
                "active_count": len(active_ids),
                "active_tasks": active_ids,
                "summary": f"Stream opened for task {task_id[:8]} (total active: {len(active_ids)})",
            })

    def update_stream_progress(self, host: str, task_id: str, bytes_transferred: int, speed_bps: float) -> None:
        """Update active byte transfer stats for ironclad verification."""
        clean = host.lower().strip()
        now = time.time()
        bumped = False
        promoted = False
        with self._lock:
            prof = self._profiles.setdefault(clean, HostConcurrencyProfile(host=clean))
            st = prof.streams.get(task_id)
            if not st or st.status != "active":
                return
            previous_bytes = int(st.bytes_transferred)
            st.bytes_transferred = bytes_transferred
            st.speed_bps = speed_bps
            st.last_active_at = now

            if prof.breaker.state == BREAKER_PROBATION:
                delta = max(0, int(bytes_transferred) - previous_bytes)
                promoted = self._note_probe_success_locked(
                    prof, task_id, bytes_delta=delta, completed_range=False, at=now
                )

            # Ironclad Rule: All concurrent streams must have transferred > 1MB and maintained speed > 0 for > 5s
            active_streams = [s for s in prof.streams.values() if s.status == "active"]
            if not prof.ceiling_locked and len(active_streams) > prof.verified_ceiling:
                all_qualify = True
                for s in active_streams:
                    dur = now - s.started_at
                    if dur < 5.0 or s.bytes_transferred < 1024 * 1024 or s.speed_bps <= 0:
                        all_qualify = False
                        break
                
                if all_qualify:
                    old_ceil = prof.verified_ceiling
                    new_ceil = len(active_streams)
                    prof.verified_ceiling = new_ceil
                    tot_speed_mb = sum(s.speed_bps for s in active_streams) / (1024 * 1024)
                    self._append_audit("PROBE_VERIFIED", clean, {
                        "previous_ceiling": old_ceil,
                        "new_verified_ceiling": new_ceil,
                        "active_tasks": [s.task_id for s in active_streams],
                        "total_speed_mbps": round(tot_speed_mb, 2),
                        "summary": f"Verified {new_ceil} concurrent streams actively transferring for >5s without error ({tot_speed_mb:.1f} MB/s total)",
                    })
                    bumped = True
        if bumped or promoted:
            self.save_profile(clean)

    def record_stream_finished(
        self,
        host: str,
        task_id: str,
        success: bool = True,
        completed_bytes: int = 0,
        expected_bytes: int = 0,
    ) -> None:
        clean = host.lower().strip()
        now = time.time()
        promoted = False
        with self._lock:
            prof = self._profiles.setdefault(clean, HostConcurrencyProfile(host=clean))
            st = prof.streams.get(task_id)
            if st:
                st.status = "completed"
            if success and prof.breaker.state == BREAKER_PROBATION:
                complete = expected_bytes <= 0 or int(completed_bytes) >= int(expected_bytes)
                if complete:
                    promoted = self._note_probe_success_locked(
                        prof, task_id, bytes_delta=0, completed_range=True, at=now
                    )
            active_ids = [s.task_id for s in prof.streams.values() if s.status == "active"]
            self._append_audit("STREAM_FINISHED", clean, {
                "task_id": task_id,
                "remaining_active": len(active_ids),
                "summary": f"Stream finished for task {task_id[:8]} ({len(active_ids)} remaining)",
            })
        if promoted:
            self.save_profile(clean)

    def record_stream_denied(
        self,
        host: str,
        failing_task_id: str,
        status_code: Optional[int] = None,
        response_text: str = "",
        cooldown_seconds: float = 300.0,
    ) -> int:
        """Record a denial (e.g. HTTP 429, 403, or 'wait' message).

        The clamp is timestamped and non-sticky: when the denial contradicts an
        established verified ceiling the ceiling is halved; when it only reveals
        a probe boundary the last safe step (attempted - 1) is kept. The
        cooldown reopen path re-arms the ladder once it elapses, and a clean
        ceiling decays +1 per 24 h without pressure.
        """
        clean = host.lower().strip()
        now = time.time()
        with self._lock:
            prof = self._profiles.setdefault(clean, HostConcurrencyProfile(host=clean))
            st = prof.streams.get(failing_task_id)
            if st is None:
                # Service-level failures identify the task, while the auditor
                # tracks one stream per resolved item as task_id:item_index.
                # Resolve that boundary so the denial is attached to the
                # actual probe stream and is durable rather than advisory.
                matching = [
                    stream for key, stream in prof.streams.items()
                    if key.startswith(f"{failing_task_id}:") and stream.status == "active"
                ]
                st = matching[0] if matching else None
            attempted_from_probe = st.attempted_concurrency if st else None
            if st:
                st.status = "denied"
            
            attempted = attempted_from_probe or (len([s for s in prof.streams.values() if s.status == "active"]) + 1)
            prof.denied_at_ceiling = attempted
            if attempted <= prof.verified_ceiling:
                new_clamped = max(1, prof.verified_ceiling // 2)
            else:
                new_clamped = max(1, attempted - 1)
            prof.verified_ceiling = new_clamped
            prof.ceiling_set_at = now
            prof.cooldown_until = now + cooldown_seconds
            
            snippet = response_text.replace("\n", " ").strip()[:180]
            self._append_audit("PROBE_DENIED", clean, {
                "failing_task_id": failing_task_id,
                "attempted_concurrency": attempted,
                "clamped_max_concurrent": new_clamped,
                "http_status": status_code,
                "cooldown_seconds": cooldown_seconds,
                "error_snippet": snippet,
                "summary": f"Concurrency {attempted} denied by host (HTTP {status_code}) — clamped to {new_clamped}",
            })
        self.save_profile(clean)
        return new_clamped

    def detect_preemption(self, host: str, triggering_task_id: str) -> bool:
        """Check if any previously active stream stopped receiving bytes right after triggering_task started."""
        clean = host.lower().strip()
        now = time.time()
        preempted = False
        with self._lock:
            prof = self._profiles.get(clean)
            if not prof:
                return False
            trig_st = prof.streams.get(triggering_task_id)
            if not trig_st:
                return False
            
            # Preemption means the host KILLED an established stream, which is very
            # different from a stream going quiet while a sibling saturates the
            # link. Six seconds of idle is completely normal at ~90 MB/s, so that
            # threshold labelled ordinary sharing as preemption, hard-clamped the
            # host to one stream and recorded a denial boundary that (because the
            # expiry path lives in `admission_decision`, which single-item package
            # parts never reach) was permanent. One unlucky run then taught the
            # system that this host allows a single stream, forever.
            #
            # Require an idle gap long enough that no ordinary sharing pattern
            # explains it, and only clamp to what is actually running.
            for st in prof.streams.values():
                if st.task_id != triggering_task_id and st.status == "active":
                    idle_seconds = now - st.last_active_at
                    if idle_seconds > _PREEMPTION_IDLE_SECONDS and (now - trig_st.started_at) > 3.0:
                        st.status = "preempted"
                        survivors = sum(
                            1 for other in prof.streams.values()
                            if other.status == "active" and other.task_id != st.task_id
                        )
                        prof.verified_ceiling = max(1, survivors)
                        prof.denied_at_ceiling = max(2, survivors + 1)
                        self._append_audit("STREAM_PREEMPTION_DETECTED", clean, {
                            "triggering_task": triggering_task_id,
                            "preempted_task": st.task_id,
                            "stalled_seconds": round(now - st.last_active_at, 1),
                            "clamped_max_concurrent": prof.verified_ceiling,
                            "idle_seconds": round(idle_seconds, 1),
                            "summary": (
                                f"Host killed existing stream {st.task_id[:8]} when new stream "
                                f"{triggering_task_id[:8]} opened (idle {idle_seconds:.0f}s) — "
                                f"clamped to {prof.verified_ceiling}"
                            ),
                        })
                        preempted = True
                        break
        if preempted:
            self.save_profile(clean)
            return True
        return False


    def record_response_headers(self, host: str, task_id: str, headers: dict[str, Any]) -> None:
        """Inspect and log host response headers for rate limits, daily quotas, or retry cooldowns."""
        clean = host.lower().strip()
        header_map = {str(k).lower(): str(v) for k, v in headers.items()}
        limit_signals: dict[str, str] = {}
        for k, v in header_map.items():
            if any(term in k for term in ("rate", "limit", "quota", "retry-after", "remain", "bandwidth", "reset", "server-timing", "cloudflare")):
                limit_signals[k] = v

        if limit_signals:
            self._append_audit("HOST_HEADER_CONSTRAINTS", clean, {
                "task_id": task_id,
                "signals": limit_signals,
                "summary": f"Observed {len(limit_signals)} rate/quota headers on {clean}",
            })

        # Dynamic cooldown parsing from Retry-After and rate limit reset headers
        now = time.time()
        delay = 0.0
        reason_found = ""

        # 1. Retry-After (seconds or RFC 2822 HTTP date)
        retry_val = header_map.get("retry-after")
        if retry_val:
            val = retry_val.strip()
            try:
                d = float(val)
                if d > delay:
                    delay = max(0.0, d)
                    reason_found = f"Retry-After: {val}s"
            except ValueError:
                try:
                    target_dt = parsedate_to_datetime(val)
                    if target_dt.tzinfo is not None:
                        d = (target_dt - datetime.now(timezone.utc)).total_seconds()
                    else:
                        d = (target_dt - datetime.utcnow()).total_seconds()
                    if d > delay:
                        delay = max(0.0, d)
                        reason_found = f"Retry-After date: {val}"
                except Exception as err:
                    logger.debug("unparseable Retry-After date %r: %s", val, err)

        # 2. Rate limit reset headers (delta seconds or epoch timestamps)
        for rk in ("x-ratelimit-reset", "ratelimit-reset", "x-rate-limit-reset", "reset"):
            if rk in header_map:
                val = header_map[rk].strip()
                try:
                    num = float(val)
                    if num > 1e9:  # Future epoch timestamp
                        d = max(0.0, num - now)
                        if d > delay:
                            delay = d
                            reason_found = f"{rk} (epoch {int(num)})"
                    elif num > 0:  # Delta seconds
                        if num > delay:
                            delay = num
                            reason_found = f"{rk} ({num}s)"
                except ValueError:
                    try:
                        target_dt = parsedate_to_datetime(val)
                        if target_dt.tzinfo is not None:
                            d = (target_dt - datetime.now(timezone.utc)).total_seconds()
                        else:
                            d = (target_dt - datetime.utcnow()).total_seconds()
                        if d > delay:
                            delay = max(0.0, d)
                            reason_found = f"{rk} date: {val}"
                    except Exception as err:
                        logger.debug("unparseable %s date %r: %s", rk, val, err)

        # Impose cooldown if a positive delay was parsed (capped at 7 days for safety).
        # Any Retry-After is authoritative host pressure and also trips the breaker.
        if delay > 0:
            delay = min(delay, 7 * 86400.0)
            should_save = False
            with self._lock:
                prof = self._profiles.setdefault(clean, HostConcurrencyProfile(host=clean))
                prof.cooldown_until = max(prof.cooldown_until, now + delay)
                reason_msg = f"Host cooldown imposed via header {reason_found}: {int(delay)}s"
                prof.rejection_reasons.append(reason_msg)
                self._append_audit("HOST_COOLDOWN_IMPOSED", clean, {
                    "task_id": task_id,
                    "delay_seconds": round(delay, 2),
                    "reason": reason_found,
                    "cooldown_until": prof.cooldown_until,
                    "summary": f"Cooldown of {int(delay)}s imposed on {clean} via {reason_found}",
                })
                self._trip_breaker_locked(
                    prof,
                    "retry_after",
                    status_code=None,
                    retry_after=delay,
                    attempted=None,
                    task_id=task_id,
                )
                should_save = True
            if should_save:
                self.save_profile(clean)

    def record_speed_dip(
        self, host: str, task_id: str, current_speed_bps: float, prev_speed_bps: float, reason: str = ""
    ) -> None:
        """Record a diagnostic speed drop. Speed dips are purely observational."""
        clean = host.lower().strip()
        drop_pct = round(((prev_speed_bps - current_speed_bps) / max(prev_speed_bps, 1.0)) * 100, 1)
        self._append_audit("SPEED_DIP_OBSERVED", clean, {
            "task_id": task_id,
            "current_speed_mbps": round(current_speed_bps / (1024 * 1024), 3),
            "prev_speed_mbps": round(prev_speed_bps / (1024 * 1024), 3),
            "drop_percent": drop_pct,
            "reason": reason or "Throughput dropped significantly during concurrent transfer",
            "summary": f"Speed dip on task {task_id[:8]}: {prev_speed_bps / (1024*1024):.2f} -> {current_speed_bps / (1024*1024):.2f} MB/s (-{drop_pct}%)",
        })




# Global singleton
concurrency_auditor = HostConcurrencyAuditor()
