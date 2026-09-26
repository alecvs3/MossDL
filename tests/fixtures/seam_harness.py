"""In-Memory Engine Seam Harness for Boundary Integration Testing.

Provides an isolated, fast test fixture that instantiates a live EngineService
against an isolated temporary data directory, with mocked external I/O boundaries
(network adapters and browser solver daemon) while running real internal
scheduling (_pump), state transitions, and concurrency gating.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from engine.models import DownloadTask
from engine.service import EngineService


class EngineSeamHarness:
    """Fixture providing a managed EngineService instance for seam testing."""

    def __init__(self, temp_dir: Optional[Path] = None) -> None:
        self._owns_temp = temp_dir is None
        self.temp_dir = temp_dir or Path(tempfile.mkdtemp(prefix="engine_seam_"))
        self.service: Optional[EngineService] = None
        self._patches: list[Any] = []

    def __enter__(self) -> "EngineSeamHarness":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def start(self) -> EngineService:
        """Start the engine service with mocked external boundaries."""
        # Ensure clean directories
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "logs").mkdir(parents=True, exist_ok=True)

        # Mock loopback solver and clipboard watcher to prevent OS hooks
        p1 = patch("engine.service.ClipboardWatcher.start", return_value=None)
        p2 = patch("engine.browser_solver.solver_daemon.solve_challenge_sync", return_value={"success": True, "turnstile_token": "mock_tok_123", "cookies": {"cf_clearance": "mock_cf_abc"}})
        for p in (p1, p2):
            p.start()
            self._patches.append(p)

        self.service = EngineService(self.temp_dir)
        return self.service

    def add_task(
        self,
        url: str,
        display_name: Optional[str] = None,
        folder_path: Optional[str] = None,
        state: str = "queued",
        priority: int = 0,
        **kwargs: Any,
    ) -> DownloadTask:
        """Helper to create and persist a real DownloadTask in the harness store."""
        if not self.service:
            raise RuntimeError("Harness must be started before adding tasks")
        task = DownloadTask(
            source_url=url,
            destination=str(self.temp_dir / "downloads"),
            display_name=display_name or Path(url).name or "test_file",
            folder_path=folder_path or str(self.temp_dir / "downloads"),
            state=state,
            priority=priority,
            **kwargs,
        )
        self.service.store.save(task)
        return task

    def wait_for_condition(self, predicate: Any, timeout: float = 3.0, interval: float = 0.05) -> bool:
        """Poll until predicate returns truthy or timeout expires."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return bool(predicate())

    def wait_for_task_state(self, task_id: str, expected_state: str, timeout: float = 3.0) -> bool:
        """Wait until task reaches the expected state."""
        def check() -> bool:
            t = self.service.store.get(task_id) if self.service else None
            return t is not None and t.state == expected_state
        return self.wait_for_condition(check, timeout=timeout)

    def close(self) -> None:
        """Cleanly shut down the service, event loop, and temp directories."""
        if self.service:
            try:
                self.service.close()
            except Exception:
                pass
            self.service = None

        for p in reversed(self._patches):
            try:
                p.stop()
            except Exception:
                pass
        self._patches.clear()

        if self._owns_temp and self.temp_dir.exists():
            try:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            except Exception:
                pass
