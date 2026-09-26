"""Durable workflow state transitions without tying the API to one executor."""

from __future__ import annotations

from typing import Any

from .platform import WorkflowJob


TERMINAL = {"completed", "failed", "canceled"}
ALLOWED = {
    "queued": {"running", "canceled"},
    "running": {"paused", "completed", "failed", "canceled", "queued"},
    "paused": {"running", "canceled", "queued"},
    "failed": {"queued", "canceled"},
    "completed": set(),
    "canceled": set(),
}


def transition(job: WorkflowJob, state: str, *, error: str | None = None) -> WorkflowJob:
    if state not in ALLOWED.get(job.state, set()):
        raise ValueError(f"workflow transition {job.state} -> {state} is not allowed")
    job.state, job.error, job.revision = state, error, job.revision + 1
    job.updated_at = __import__("time").time()
    return job


def progress(job: WorkflowJob, completed: int, total: int) -> WorkflowJob:
    job.progress = 1.0 if total <= 0 else max(0.0, min(1.0, completed / total))
    job.updated_at = __import__("time").time()
    return job


def make_job(kind: str, steps: list[dict[str, Any]] | None = None, job_id: str | None = None) -> WorkflowJob:
    return WorkflowJob(job_id or __import__("uuid").uuid4().hex, kind, steps=steps or [])
