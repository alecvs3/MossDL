"""Which solvers may try a challenge, decided from what each declares it can do.

Every solver states its contract: the challenge types it answers, the inputs
it needs, what it returns, and whether it is production-grade, shadow-only
(OCR, until measured) or a person. The router checks each contract and the
solver's health against the challenge and gives every candidate a disposition
with a stable reason code. Nothing is skipped silently, and a solver is never
tried just because it is switched on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .telemetry import telemetry_bus

# Stable reason codes (logged, tested, shown in diagnostics).
ELIGIBLE = "eligible"
MANUAL = "manual_fallback"
SHADOW_ONLY = "shadow_only"
NO_DECLARATION = "no_capability_declared"
DISABLED = "disabled"
UNSUPPORTED_TYPE = "unsupported_type"
MISSING_INPUT = "missing_input"
UNHEALTHY = "unhealthy"
AUTOMATION_OFF = "automation_off"
TYPE_OFF = "type_disabled_by_user"
DECLINED = "declined_by_solver"

IMAGE_INPUTS = frozenset({"image_data", "image_url", "image_base64", "image_path", "image_bytes", "text_prompt", "raw_text", "mock_solution"})
PAGE_INPUTS = frozenset({"page_url", "url"})
SITEKEY_INPUTS = frozenset({"site_key", "sitekey"})


@dataclass(frozen=True)
class SolverCapability:
    challenge_types: frozenset[str]
    output: str                     # "token" | "text" | "clearance" | "answer"
    artifact_scope: str             # a challenge_artifacts kind
    # Per challenge type (or "*"), groups of inputs; each group needs one present.
    inputs: dict[str, tuple[frozenset[str], ...]] = field(default_factory=dict)
    identity_bound: bool = False    # must run on the task's own route and identity
    production: bool = True         # False: runs only as a measured shadow
    manual: bool = False            # a person answers

    def missing_input(self, challenge_type: str, params: dict[str, Any]) -> str | None:
        for group in self.inputs.get(challenge_type, self.inputs.get("*", ())):
            if not any(params.get(name) for name in group):
                return "/".join(sorted(group))
        return None


@dataclass(frozen=True)
class Health:
    healthy: bool
    reason: str = ""


@dataclass(frozen=True)
class RouteDecision:
    solver_id: str
    disposition: str   # "try" | "shadow" | "manual" | "rejected"
    reason: str
    detail: str = ""


@dataclass
class RoutePlan:
    challenge_id: str
    automated: list[Any] = field(default_factory=list)
    shadow: list[Any] = field(default_factory=list)
    manual: list[Any] = field(default_factory=list)
    decisions: list[RouteDecision] = field(default_factory=list)

    @property
    def manual_required(self) -> bool:
        return not self.automated


def _type_value(challenge: Any) -> str:
    kind = challenge.captcha_type
    return str(getattr(kind, "value", kind))


def route(challenge: Any, solvers: Iterable[Any], *, automation_enabled: bool, type_enabled: bool) -> RoutePlan:
    """Decide, for every solver, whether it may try this challenge and why."""
    plan = RoutePlan(challenge_id=challenge.id)
    ctype = _type_value(challenge)
    params = challenge.params or {}
    for solver in solvers:
        declare = getattr(solver, "capability", None)
        cap = declare() if callable(declare) else None
        detail = ""
        if cap is None:
            reason = NO_DECLARATION
        elif not solver.enabled:
            reason = DISABLED
        elif ctype not in cap.challenge_types:
            reason = UNSUPPORTED_TYPE
        elif (missing := cap.missing_input(ctype, params)) is not None:
            reason, detail = MISSING_INPUT, missing
        elif not (health := solver.health()).healthy:
            reason, detail = UNHEALTHY, health.reason
        elif cap.manual:
            reason = MANUAL
        elif not cap.production:
            reason = SHADOW_ONLY
        elif not automation_enabled:
            reason = AUTOMATION_OFF
        elif not type_enabled:
            reason = TYPE_OFF
        elif not solver.can_solve(challenge):
            reason = DECLINED
        else:
            reason = ELIGIBLE
        disposition = {ELIGIBLE: "try", SHADOW_ONLY: "shadow", MANUAL: "manual"}.get(reason, "rejected")
        {"try": plan.automated, "shadow": plan.shadow, "manual": plan.manual}.get(disposition, []).append(solver)
        plan.decisions.append(RouteDecision(solver.solver_id, disposition, reason, detail))
    telemetry_bus.record(
        level="INFO", subsystem="engine:captcha",
        message=f"[CAPTCHA_ROUTE] {ctype} {challenge.id}: {len(plan.automated)} to try, "
                f"{len(plan.shadow)} shadow, {len(plan.manual)} manual",
        context={"challenge_id": challenge.id, "type": ctype,
                 "decisions": [d.__dict__ for d in plan.decisions]},
        tier="engine",
    )
    return plan
