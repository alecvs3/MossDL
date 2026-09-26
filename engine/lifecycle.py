"""Deterministic per-task lifecycle stage machine.

The task *state* (queued/resolving/downloading/...) is scheduler truth. The
task *stage* is the hoster-facing lifecycle position (metadata resolve, wait
timer, CAPTCHA phases, direct link, transfer, integrity verify, archive
pipeline, cleanup). Every stage change is validated against a legal-transition
table, persisted with a microsecond-precision timestamp, and emitted as a
``TaskStageChanged`` event. Illegal moves are never silently applied: the
disqualifying condition is logged in structured telemetry before the
authoritative override starts a new lifecycle epoch.
"""

from __future__ import annotations

import time
from typing import Any, Literal

TaskStage = Literal[
    "resolving_metadata", "hoster_wait_timer", "captcha_challenge_detected",
    "captcha_solving", "captcha_verifying", "direct_link_acquired",
    "downloading", "verifying_integrity", "unraring_pending",
    "unraring_extracting", "archive_cleanup", "completed", "failed",
]

STAGE_LABELS: dict[str, str] = {
    "resolving_metadata": "Resolving metadata",
    "hoster_wait_timer": "Waiting timer",
    "captcha_challenge_detected": "CAPTCHA detected",
    "captcha_solving": "Solving CAPTCHA",
    "captcha_verifying": "Verifying CAPTCHA",
    "direct_link_acquired": "Direct link acquired",
    "downloading": "Downloading",
    "verifying_integrity": "Verifying integrity",
    "unraring_pending": "Waiting for archive parts",
    "unraring_extracting": "Extracting archive",
    "archive_cleanup": "Cleaning up archives",
    "completed": "Completed",
    "failed": "Failed",
}

# Deterministic legal stage transitions. A stage may always re-enter itself
# (detail refreshes, e.g. per-second countdown ticks). ``failed`` may re-enter
# the early stages because the retry policy recycles the task through
# resolution; ``completed`` may enter the archive pipeline because multipart
# extraction is a post-completion lifecycle continuation.
STAGE_TRANSITIONS: dict[str, set[str]] = {
    "resolving_metadata": {"hoster_wait_timer", "captcha_challenge_detected", "captcha_solving",
                           "captcha_verifying", "direct_link_acquired", "downloading", "failed"},
    "hoster_wait_timer": {"resolving_metadata", "captcha_challenge_detected", "captcha_solving",
                          "captcha_verifying", "direct_link_acquired", "downloading", "failed"},
    # Every CAPTCHA stage can return to ``resolving_metadata``: a solve hands the
    # clearance back to the resolver, which re-runs resolution to obtain the link.
    # Omitting those edges did not prevent the move -- it forced a new lifecycle
    # epoch on each one, so the persisted stage history no longer described the
    # path the task actually took.
    "captcha_challenge_detected": {"resolving_metadata", "captcha_solving", "captcha_verifying",
                                   "direct_link_acquired", "downloading", "failed"},
    "captcha_solving": {"resolving_metadata", "captcha_verifying", "direct_link_acquired",
                        "downloading", "failed"},
    "captcha_verifying": {"resolving_metadata", "hoster_wait_timer", "captcha_challenge_detected",
                          "captcha_solving", "direct_link_acquired", "downloading", "failed"},
    "direct_link_acquired": {"downloading", "failed"},
    "downloading": {"verifying_integrity", "unraring_pending", "unraring_extracting",
                    "archive_cleanup", "completed", "failed"},
    "verifying_integrity": {"downloading", "unraring_pending", "unraring_extracting",
                            "archive_cleanup", "completed", "failed"},
    "unraring_pending": {"unraring_extracting", "archive_cleanup", "completed", "failed"},
    "unraring_extracting": {"verifying_integrity", "archive_cleanup", "completed", "failed"},
    "archive_cleanup": {"completed", "failed"},
    # ``archive_cleanup`` belongs here for the same reason as the other archive
    # stages: source cleanup is a post-completion continuation of the package.
    "completed": {"unraring_pending", "unraring_extracting", "verifying_integrity",
                  "archive_cleanup", "failed"},
    "failed": {"resolving_metadata", "hoster_wait_timer", "captcha_challenge_detected",
               "captcha_solving", "captcha_verifying", "direct_link_acquired",
               "downloading", "verifying_integrity"},
}

# Scheduler state -> default lifecycle stage. States mapped to ``None`` never
# touch the stage: queued/paused/needs_user/pending_probe are waiting positions,
# not lifecycle moves.
STATE_STAGE_SYNC: dict[str, str | None] = {
    "queued": None,
    "pending_probe": None,
    "resolving": "resolving_metadata",
    "retrying": "resolving_metadata",
    "preflight": "direct_link_acquired",
    "downloading": "downloading",
    "verifying": "verifying_integrity",
    "postprocessing": None,
    "needs_user": None,
    "paused": None,
    "canceled": None,
    "completed": "completed",
    "failed": "failed",
}

# Ad-hoc solver stage strings -> canonical lifecycle stage.
# Solver steps that are genuine TASK-level milestones and may move the
# lifecycle stage. Everything else the browser solver does happens *inside* a
# solve and belongs in `stage_detail.solver_step`, not on the task's stage.
#
# Mapping every internal step onto the top-level stage made a single solve
# rewrite the task stage five times, interleaved with the engine's own resolve
# transitions. The UI showed the browser's micro-walk as if it were the task's
# macro-progress: solving -> getting metadata -> solving -> waiting timer ->
# getting metadata, for one CAPTCHA. Measured churn was 10-21 lifecycle
# transitions per part where four suffice.
SOLVER_STAGE_MAP: dict[str, str] = {
    # A real wait the user is entitled to see, with a countdown.
    "countdown": "hoster_wait_timer",
    # The solve produced a usable link: genuine forward progress.
    "primed": "direct_link_acquired",
}

# Progress within a solve. These refresh `stage_detail` and never change stage.
SOLVER_DETAIL_STEPS: frozenset[str] = frozenset({
    "navigating",
    "step2_clicked",
    "step_advance",
    "turnstile_detected",
    "turnstile_solved",
})

# CAPTCHA coordinator states (captcha.py challenge params) -> lifecycle stage.
COORDINATOR_STAGE_MAP: dict[str, str] = {
    "clearcote_pending": "captcha_solving",
    "clearcote_active": "captcha_solving",
    "manual_required": "captcha_challenge_detected",
}

# Archive job events -> lifecycle stage. ``None`` means the event refreshes
# detail on the current stage instead of moving the machine.
ARCHIVE_EVENT_STAGE_MAP: dict[str, str | None] = {
    "ArchiveQueued": "unraring_pending",
    "ArchiveStarted": "unraring_extracting",
    "ArchiveProgress": None,
    "ArchiveVerifying": "verifying_integrity",
    "ArchiveCleanupPending": "archive_cleanup",
    "ArchiveCompleted": "archive_cleanup",
    "ArchiveFailed": None,
    "ArchiveNeedsUser": None,
    "ArchiveCanceled": None,
}

MAX_STAGE_HISTORY = 50


def is_legal_transition(from_stage: str | None, to_stage: str) -> bool:
    if from_stage is None or from_stage == to_stage:
        return True
    return to_stage in STAGE_TRANSITIONS.get(from_stage, set())


def stage_label(stage: str | None) -> str:
    return STAGE_LABELS.get(stage or "", str(stage))


def now_us() -> float:
    """Wall-clock seconds with sub-microsecond float resolution."""
    return time.time()


def format_ts_us(timestamp: float) -> str:
    return f"{timestamp:.6f}"


def history_entry(from_stage: str | None, to_stage: str, detail: dict[str, Any],
                  entered_at: float, forced: bool, source: str) -> dict[str, Any]:
    return {
        "from": from_stage,
        "to": to_stage,
        "entered_at": round(entered_at, 6),
        "entered_at_us": format_ts_us(entered_at),
        "forced": forced,
        "source": source,
        "detail": _safe_detail(detail),
    }


def _safe_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Redacts bearer material from stage detail before persisting/emitting."""
    sensitive = {"token", "turnstile_token", "cf-turnstile-response", "solution",
                 "direct_url", "url", "cookies", "password", "api_token", "access_token"}
    return {key: (None if str(key).lower() in sensitive else value)
            for key, value in (detail or {}).items()}


def solver_stage_detail(stage: str, data: dict[str, Any]) -> dict[str, Any]:
    detail: dict[str, Any] = {"source": "solver"}
    if "countdown_seconds" in data:
        detail["countdown_seconds"] = data["countdown_seconds"]
        detail["countdown_captured_at"] = time.time()
    if stage == "direct_link_acquired":
        detail["primed"] = True
    return detail


def archive_event_detail(event_type: str, job_dict: dict[str, Any]) -> dict[str, Any]:
    detail: dict[str, Any] = {"source": "archive", "event": event_type}
    for key in ("operation", "state", "progress_bytes", "expected_size", "observed_size", "error"):
        if key in job_dict and job_dict[key] is not None:
            detail[key] = job_dict[key]
    return detail
