"""Read-only shaping of captcha, shortlink, and per-download activity history.

The engine already persists every captcha challenge, shortlink hop, lifecycle
stage, provider attempt, and diagnostic.  These helpers turn those rows into
UI-ready records without exposing tokens, cookies, or signed URLs.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from . import lifecycle
from .collection import public_url

# A pre-transfer stage held longer than this is reported as a stall.
STALL_SECONDS = 45.0
# Stages that are expected to take a while and are never a stall.
_STALL_EXEMPT_STAGES = {
    "downloading", "verifying_integrity", "unraring_extracting", "archive_cleanup",
    "hoster_wait_timer", "completed", "failed",
}
# Task state changes worth calling out; routine ones (and completion) are covered by stages.
_NOTABLE_STATES = {"paused", "failed", "canceled", "retrying"}
# Diagnostic stages that duplicate the state/stage rows or describe engine internals.
_INTERNAL_DIAGNOSTIC_STAGES = {"state", "lifecycle"}

_SOLVER_LABELS = {
    "clearcote": "Clearcote",
    "automated_browser": "Clearcote",
    "interactive_ui": "You",
    "browser_loopback": "Your browser",
    "twocaptcha": "2Captcha",
    "anticaptcha": "Anti-Captcha",
    "flaresolverr": "FlareSolverr",
    "audio_speech": "Audio solver",
    "local_ocr": "Local OCR",
    "ddddocr": "Local OCR",
    "darknet_yolo": "Vision model",
}


def captcha_vendor(captcha_type: str | None) -> str:
    """Company behind a captcha type, used to pick the logo in the UI."""
    value = str(captcha_type or "").lower().removeprefix("captchatype.")
    if value in {"turnstile", "browser_session"}:
        return "cloudflare"
    if value.startswith("recaptcha"):
        return "google"
    if value.startswith("hcaptcha"):
        return "hcaptcha"
    return "image"


def captcha_type_label(captcha_type: str | None) -> str:
    value = str(captcha_type or "").lower().removeprefix("captchatype.")
    return {
        "turnstile": "Cloudflare Turnstile",
        "browser_session": "Cloudflare challenge",
        "recaptcha_v2": "reCAPTCHA v2",
        "recaptcha_v3": "reCAPTCHA v3",
        "recaptcha_audio": "reCAPTCHA audio",
        "hcaptcha": "hCaptcha",
        "hcaptcha_audio": "hCaptcha audio",
        "image_text": "Image captcha",
        "positional_click": "Click captcha",
    }.get(value, value.replace("_", " ") or "Captcha")


def solver_label(solver_id: str | None) -> str | None:
    if not solver_id:
        return None
    return _SOLVER_LABELS.get(solver_id, solver_id.replace("_", " ").title())


def captcha_outcome(status: str | None, expires_at: float | None, now: float | None = None) -> str:
    """Collapse engine statuses into solved / failed / stalled / skipped / pending."""
    value = str(status or "pending").lower()
    if value == "solved":
        return "solved"
    if value == "failed":
        return "failed"
    if value == "expired":
        return "stalled"
    if value == "skipped":
        return "skipped"
    if value == "verifying":
        return "pending"  # answered; the site has not ruled yet
    # A pending row that outlived its deadline was abandoned, not live.
    if expires_at is not None and float(expires_at) < (now if now is not None else time.time()):
        return "stalled"
    return "pending"


def _page_url(params: dict[str, Any]) -> str | None:
    url = params.get("page_url") or params.get("url")
    return public_url(str(url)) if url else None


def captcha_history_entry(row: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Public shape for one stored challenge. Never includes params or solutions."""
    params = row.get("params") or {}
    created = row.get("created_at")
    resolved = row.get("resolved_at")
    page_url = _page_url(params)
    return {
        "id": row["id"],
        "task_id": row.get("task_id"),
        "task_name": row.get("task_name") or row.get("task_url"),
        "provider_id": row.get("provider_id"),
        "host": params.get("host") or params.get("origin_host"),
        "captcha_type": str(row.get("captcha_type") or "").lower().removeprefix("captchatype."),
        "type_label": captcha_type_label(row.get("captcha_type")),
        "vendor": captcha_vendor(row.get("captcha_type")),
        "status": row.get("status"),
        "outcome": captcha_outcome(row.get("status"), row.get("expires_at"), now),
        "solver_id": row.get("solver_id") or row.get("solver_used"),
        "solver_label": solver_label(row.get("solver_id") or row.get("solver_used")),
        "error": row.get("error"),
        "created_at": created,
        "resolved_at": resolved,
        "duration_seconds": (float(resolved) - float(created)) if resolved and created else None,
        "retry_count": int(row.get("retry_count") or 0),
        "page_url": page_url,
        "group_id": params.get("group_id"),
    }


def shortlink_history(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group stored hops by download, most recently active first."""
    chains: dict[str, dict[str, Any]] = {}
    for row in rows:
        chain = chains.setdefault(row["task_id"], {
            "task_id": row["task_id"],
            "task_name": row.get("task_name") or row.get("task_url"),
            "task_state": row.get("task_state"),
            "updated_at": 0.0,
            "hops": [],
        })
        chain["hops"].append({
            "hop": int(row["hop"]),
            "host": row.get("host") or "",
            "provider": row.get("provider"),
            "state": row.get("state"),
            "error": row.get("error"),
            "at": row.get("updated_at"),
        })
        chain["updated_at"] = max(chain["updated_at"], float(row.get("updated_at") or 0.0))
    result = list(chains.values())
    for chain in result:
        chain["hops"].sort(key=lambda hop: hop["hop"])
        chain["failed"] = any(hop.get("error") or hop.get("state") == "failed" for hop in chain["hops"])
    result.sort(key=lambda chain: chain["updated_at"], reverse=True)
    return result


def _stage_events(stage_history: list[dict[str, Any]], end_at: float) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, entry in enumerate(stage_history):
        stage = str(entry.get("to") or "")
        entered = entry.get("entered_at")
        if not stage or entered is None or stage == "failed":
            # Failure is reported by its state transition, which carries the reason.
            continue
        following = next((item.get("entered_at") for item in stage_history[index + 1:]
                          if item.get("entered_at") is not None), None)
        held = max(0.0, float(following if following is not None else end_at) - float(entered))
        stalled = stage not in _STALL_EXEMPT_STAGES and held >= STALL_SECONDS
        detail = entry.get("detail") or {}
        tone = "success" if stage == "completed" else "error" if stage == "failed" else (
            "warning" if stalled else "neutral")
        events.append({
            "at": float(entered),
            "kind": "stage",
            "title": lifecycle.stage_label(stage),
            "stage": stage,
            "duration_seconds": held,
            "stalled": stalled,
            "tone": tone,
            "detail": detail.get("manual_reason") or detail.get("reason"),
        })
    return events


def build_task_timeline(task: Any, *, transitions: list[dict[str, Any]],
                        challenges: list[dict[str, Any]], chain: list[dict[str, Any]],
                        attempts: list[dict[str, Any]], diagnostics: list[dict[str, Any]],
                        now: float | None = None) -> dict[str, Any]:
    """Merge everything recorded about one download into a single ordered timeline."""
    now = time.time() if now is None else now
    end_at = float(task.finished_at or now)
    events = _stage_events(list(task.stage_history or []), end_at)

    for row in challenges:
        entry = captcha_history_entry(row, now)
        solver = entry["solver_label"]
        verb = {"solved": "solved", "failed": "failed", "stalled": "stalled",
                "skipped": "skipped", "pending": "waiting"}[entry["outcome"]]
        events.append({
            "at": float(entry["created_at"] or 0.0),
            "kind": "captcha",
            "title": f"{entry['type_label']} {verb}" + (f" by {solver}" if solver and verb == "solved" else ""),
            "tone": {"solved": "success", "failed": "error", "stalled": "warning"}.get(entry["outcome"], "neutral"),
            "duration_seconds": entry["duration_seconds"],
            "detail": entry["error"],
            "captcha": entry,
        })

    for hop in chain:
        failed = bool(hop.get("error")) or hop.get("state") == "failed"
        events.append({
            "at": float(hop.get("updated_at") or 0.0),
            "kind": "shortlink",
            "title": f"Shortlink step {int(hop['hop']) + 1}: {hop.get('host') or 'unknown host'}",
            "tone": "error" if failed else "neutral",
            "detail": hop.get("error") or hop.get("state"),
            "hop": {"hop": int(hop["hop"]), "host": hop.get("host"), "state": hop.get("state"),
                    "provider": hop.get("provider")},
        })

    for attempt in attempts:
        outcome = str(attempt.get("outcome") or "")
        ok = outcome in {"resolved", "completed", "success"}
        started = attempt.get("started_at")
        ended = attempt.get("ended_at")
        events.append({
            "at": float(started or 0.0),
            "kind": "provider",
            "title": f"{attempt.get('provider_id') or 'Provider'}: {outcome.replace('_', ' ') or 'attempted'}",
            "tone": "success" if ok else "error" if outcome == "failed" else (
                "warning" if attempt.get("error") else "neutral"),
            "duration_seconds": (float(ended) - float(started)) if ended and started else None,
            "detail": attempt.get("error") or (f"HTTP {attempt['status_code']}" if attempt.get("status_code") else None),
        })

    previous: tuple[Any, Any] | None = None
    for transition in sorted(transitions, key=lambda row: float(row.get("created_at") or 0.0)):
        state = transition.get("to_state")
        key = (state, transition.get("reason"))
        if state not in _NOTABLE_STATES or key == previous:
            previous = key
            continue
        previous = key
        events.append({
            "at": float(transition.get("created_at") or 0.0),
            "kind": "state",
            "title": {"paused": "Paused", "failed": "Failed", "canceled": "Cancelled",
                      "retrying": "Retrying"}[state],
            "tone": {"failed": "error", "retrying": "warning"}.get(state, "neutral"),
            "detail": transition.get("reason"),
        })

    for diagnostic in diagnostics:
        level = str(diagnostic.get("level") or "").lower()
        if level not in {"warn", "warning", "error", "fatal"}:
            continue
        if str(diagnostic.get("stage") or "") in _INTERNAL_DIAGNOSTIC_STAGES:
            continue
        events.append({
            "at": float(diagnostic.get("created_at") or 0.0),
            "kind": "log",
            "title": str(diagnostic.get("message") or "")[:240],
            "tone": "error" if level in {"error", "fatal"} else "warning",
            "detail": diagnostic.get("stage"),
        })

    events.sort(key=lambda event: event["at"])
    stalls = [event for event in events if event.get("stalled")]
    started_at = events[0]["at"] if events else None
    captcha_entries = [event["captcha"] for event in events if event["kind"] == "captcha"]
    return {
        "task_id": task.id,
        "state": task.state,
        "error": task.error,
        "started_at": started_at,
        "finished_at": task.finished_at,
        "duration_seconds": (end_at - started_at) if started_at else None,
        "captchas": {
            "total": len(captcha_entries),
            "solved": sum(1 for entry in captcha_entries if entry["outcome"] == "solved"),
            "failed": sum(1 for entry in captcha_entries if entry["outcome"] in {"failed", "stalled"}),
        },
        "shortlink_hops": len(chain),
        "stalled_at": max(stalls, key=lambda event: event["duration_seconds"])["title"] if stalls else None,
        "events": events,
    }


def activity_counts(captcha_rows: Iterable[dict[str, Any]],
                    hop_rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per-download badge counts for the History list, without loading timelines."""
    counts: dict[str, dict[str, int]] = {}
    for row in captcha_rows:
        entry = counts.setdefault(row["task_id"], {"captchas": 0, "captchas_failed": 0, "hops": 0})
        entry["captchas"] += int(row.get("total") or 0)
        entry["captchas_failed"] += int(row.get("failed") or 0)
    for row in hop_rows:
        entry = counts.setdefault(row["task_id"], {"captchas": 0, "captchas_failed": 0, "hops": 0})
        entry["hops"] += int(row.get("hops") or 0)
    return counts
