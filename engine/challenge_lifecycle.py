"""The engine's own record of where a challenge stands, and what may happen next.

Each challenge carries a lifecycle: a state, a generation that increases with
every fresh attempt, and a short history. It only moves along legal
transitions, it is saved with the challenge (so it survives a restart), and
commands from the UI name the generation they were issued for, so a stale or
repeated click gets an explicit answer instead of acting on the wrong attempt.
The UI renders the projection from here; it never infers state on its own.
"""
from __future__ import annotations

import time
from typing import Any

from .telemetry import telemetry_bus

DETECTED, ROUTING, SOLVING, MANUAL, VERIFYING = "detected", "routing", "solving", "manual_required", "verifying"
RESOLVED, REJECTED, PARKED = "resolved", "rejected", "parked"
EXPIRED, SKIPPED, CANCELLED = "expired", "skipped", "cancelled"
TERMINAL = frozenset({RESOLVED, EXPIRED, SKIPPED, CANCELLED})

TRANSITIONS: dict[str, frozenset[str]] = {
    DETECTED: frozenset({ROUTING, PARKED, MANUAL, VERIFYING, EXPIRED, SKIPPED, CANCELLED}),
    PARKED: frozenset({ROUTING, MANUAL, EXPIRED, SKIPPED, CANCELLED}),
    ROUTING: frozenset({SOLVING, MANUAL, EXPIRED, SKIPPED, CANCELLED}),
    SOLVING: frozenset({VERIFYING, MANUAL, EXPIRED, SKIPPED, CANCELLED}),
    MANUAL: frozenset({VERIFYING, ROUTING, EXPIRED, SKIPPED, CANCELLED}),
    VERIFYING: frozenset({RESOLVED, REJECTED, EXPIRED, CANCELLED}),
    REJECTED: frozenset({ROUTING, MANUAL, SKIPPED, CANCELLED}),
}
# What the user can do in each state (shown by the UI, never derived there).
NEXT_ACTION = {
    DETECTED: "wait", PARKED: "wait", ROUTING: "wait", SOLVING: "wait", VERIFYING: "wait",
    MANUAL: "answer", REJECTED: "answer",
}
_HISTORY = 20


def _record(challenge: Any) -> dict[str, Any]:
    params = challenge.params if challenge.params is not None else {}
    challenge.params = params
    return params.setdefault("lifecycle", {"state": DETECTED, "generation": 1, "reason": "detected",
                                           "updated_at": time.time(), "history": []})


def state_of(challenge: Any) -> str:
    return _record(challenge)["state"]


def generation_of(challenge: Any) -> int:
    return int(_record(challenge)["generation"])


def advance(challenge: Any, to: str, reason: str) -> bool:
    """Move to `to` if legal. A new attempt after a rejection bumps the generation."""
    record = _record(challenge)
    current = record["state"]
    if current == to:
        return True
    if to not in TRANSITIONS.get(current, frozenset()):
        telemetry_bus.record(level="WARN", subsystem="engine:captcha",
                             message=f"[CHALLENGE_ILLEGAL_TRANSITION] {challenge.id}: {current} -> {to} refused ({reason})",
                             context={"challenge_id": challenge.id, "from": current, "to": to, "reason": reason}, tier="engine")
        return False
    if current == REJECTED and to in {ROUTING, MANUAL}:
        record["generation"] = int(record["generation"]) + 1
    record.update(state=to, reason=reason, updated_at=time.time())
    record["history"] = (record.get("history") or [])[-(_HISTORY - 1):] + [{"to": to, "reason": reason, "at": record["updated_at"]}]
    telemetry_bus.record(level="INFO", subsystem="engine:captcha",
                         message=f"[CHALLENGE_STATE] {challenge.id} {current} -> {to} (gen {record['generation']}): {reason}",
                         context={"challenge_id": challenge.id, "task_id": getattr(challenge, "task_id", None),
                                  "from": current, "to": to, "generation": record["generation"], "reason": reason},
                         tier="engine")
    return True


def command_outcome(challenge: Any | None, generation: int | None) -> str | None:
    """Why a UI command must not act (None when it may)."""
    if challenge is None:
        return "unknown_challenge"
    record = _record(challenge)
    if generation is not None and int(generation) != int(record["generation"]):
        return "stale"
    if record["state"] in TERMINAL:
        return "closed"
    if record["state"] == VERIFYING:
        return "duplicate"
    return None


def recover_after_restart(challenge: Any) -> bool:
    """Work in flight when the engine stopped cannot be trusted: hand it to the user."""
    if state_of(challenge) in {ROUTING, SOLVING, VERIFYING}:
        record = _record(challenge)
        record.update(state=MANUAL, reason="engine restarted during the attempt; answer again", updated_at=time.time())
        record["generation"] = int(record["generation"]) + 1
        return True
    return False


def projection(challenge: Any) -> dict[str, Any]:
    """The typed view the UI renders."""
    record = _record(challenge)
    kind = getattr(challenge.captcha_type, "value", challenge.captcha_type)
    return {
        "challenge_id": challenge.id, "task_id": challenge.task_id, "provider": challenge.provider_id,
        "type": str(kind), "state": record["state"], "generation": record["generation"],
        "reason": record["reason"], "expires_at": challenge.expires_at,
        "next_action": NEXT_ACTION.get(record["state"], "none"),
    }
