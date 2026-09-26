"""Multi-Part Lifecycle Test Harness.

Drives the end-to-end download process from Linkgrabber / URL ingestion
through resolution, Clearcote automated bypass, stream downloading,
integrity verification, and folder consolidation.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import DEFAULT_DESTINATION, get_workspace_root


@dataclass
class LifecycleResult:
    url: str
    task_id: str | None
    state: str
    file_path: Path | None
    file_size: int
    duration_s: float
    error: str | None
    solver_used: str | None


class LifecycleRunner:
    """Orchestrates end-to-end multi-part download lifecycle tests."""

    def __init__(self, destination: Path | None = None) -> None:
        self.destination = destination or DEFAULT_DESTINATION
        self.workspace = get_workspace_root()

    async def run_single_part_test(
        self,
        url: str,
        timeout_seconds: float = 120.0,
        progress_cb: Callable[[str, float], None] | None = None,
    ) -> LifecycleResult:
        """Run a single-part download lifecycle through the engine."""
        from engine.service import EngineService
        from engine.store import TaskStore

        self.destination.mkdir(parents=True, exist_ok=True)
        store = TaskStore()
        service = EngineService(store)
        await service.start()

        t_start = time.perf_counter()
        task_id = None
        state = "unknown"
        error = None
        solver_used = None
        file_path = None
        file_size = 0

        try:
            # 1. Add task via engine dispatch
            task_dict = service.dispatch(
                "add_task",
                {
                    "source_url": url,
                    "destination": str(self.destination),
                    "auto_start": True,
                },
            )
            task_id = task_dict["id"]
            if progress_cb:
                progress_cb(f"Task added: {task_id}", 0.0)

            # 2. Poll until terminal state
            t_poll_start = time.perf_counter()
            while (time.perf_counter() - t_poll_start) < timeout_seconds:
                task = store.get(task_id)
                if not task:
                    await asyncio.sleep(0.5)
                    continue

                state = task.state
                if progress_cb:
                    pct = (task.completed_bytes / max(1, task.size or 1)) * 100.0 if task.size else 0.0
                    progress_cb(f"State: {state} ({pct:.1f}%)", pct)

                # If task entered needs_user (e.g. CAPTCHA or Turnstile challenge), trigger solve
                if state == "needs_user":
                    user_chal = task.user_challenge or {}
                    solver_used = user_chal.get("solver_engine", "Clearcote")
                    if progress_cb:
                        progress_cb(f"Triggering automated solver ({solver_used})...", 0.0)
                    try:
                        challenge_id = user_chal.get("id") or user_chal.get("challenge_id") or task.id
                        service.dispatch("captcha_solve", {"challenge_id": challenge_id})
                    except Exception as s_err:
                        error = f"captcha_solve dispatch error: {s_err}"

                if state in ("completed", "canceled", "failed"):
                    if state == "failed":
                        error = task.error or "Task transitioned to failed"
                    break

                await asyncio.sleep(1.0)

            # 3. Verify destination file
            task = store.get(task_id)
            if task and task.resolved:
                item = task.resolved[0]
                expected_name = item.display_name
                candidate_path = self.destination / expected_name
                if candidate_path.exists():
                    file_path = candidate_path
                    file_size = candidate_path.stat().st_size

        except Exception as exc:
            error = f"Lifecycle test exception: {exc}"
            state = "exception"
        finally:
            await service.stop()

        dur = time.perf_counter() - t_start
        return LifecycleResult(
            url=url,
            task_id=task_id,
            state=state,
            file_path=file_path,
            file_size=file_size,
            duration_s=round(dur, 2),
            error=error,
            solver_used=solver_used,
        )
