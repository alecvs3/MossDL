"""A solver's answer counts only once the site has accepted it.

Receiving an answer moves a challenge to `verifying`; the task resumes to
submit it (the provider call is where the site judges the answer). The task's
next outcome settles it: getting past the site accepts the answer and releases
its reusable clearance to coherent siblings; being challenged again rejects it,
reports the bad answer to its solver (refunds, calibration) and quarantines the
clearance it came with. Nothing here keeps the answer itself.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from . import challenge_lifecycle as lifecycle
from .challenge_artifacts import redacted_summary
from .telemetry import telemetry_bus


class ChallengeVerifier:
    def __init__(self, manager: Any, *, on_accept: Callable[[Any], None] | None = None,
                 on_reject: Callable[[Any], None] | None = None) -> None:
        self.manager = manager
        self.on_accept = on_accept
        self.on_reject = on_reject

    def _settle(self, challenge_id: str, accepted: bool, evidence: str) -> bool:
        from .captcha import CaptchaStatus
        challenge = self.manager.get_challenge(challenge_id)
        if challenge is None or lifecycle.state_of(challenge) != lifecycle.VERIFYING:
            telemetry_bus.record(level="INFO", subsystem="engine:captcha",
                                 message=f"[CHALLENGE_VERDICT_IGNORED] {challenge_id}: not awaiting a verdict",
                                 context={"challenge_id": challenge_id, "accepted": accepted}, tier="engine")
            return False
        lifecycle.advance(challenge, lifecycle.RESOLVED if accepted else lifecycle.REJECTED, evidence)
        challenge.status = CaptchaStatus.SOLVED if accepted else CaptchaStatus.FAILED
        challenge.error = None if accepted else f"The site rejected the answer: {evidence}"
        challenge.resolved_at = time.time()
        summary = redacted_summary(challenge.solution)
        if not accepted:
            challenge.solution = None  # a rejected answer is never offered again
        self.manager.report_result(challenge_id, accepted)
        self.manager._persist_challenge(challenge)
        self.manager._emit_event("CaptchaVerified" if accepted else "CaptchaRejected", challenge.task_id,
                                 {"challenge_id": challenge_id, "solver_id": challenge.solver_used, "evidence": evidence})
        telemetry_bus.record(level="INFO" if accepted else "WARN", subsystem="engine:captcha",
                             message=f"[CHALLENGE_{'ACCEPTED' if accepted else 'REJECTED'}] {challenge_id} by the site: {evidence}",
                             context={"challenge_id": challenge_id, "task_id": challenge.task_id,
                                      "solver": challenge.solver_used, "artifacts": summary}, tier="engine")
        hook = self.on_accept if accepted else self.on_reject
        if hook is not None:
            hook(challenge)
        return True

    def accept(self, challenge_id: str, evidence: str) -> bool:
        return self._settle(challenge_id, True, evidence)

    def reject(self, challenge_id: str, evidence: str) -> bool:
        return self._settle(challenge_id, False, evidence)
