from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.service import EngineService
from engine.models import DownloadTask, ResolvedItem


class TestDomainStaggering(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.service = EngineService(self.data_dir)

    async def asyncTearDown(self) -> None:
        self.service.close()
        self.temp_dir.cleanup()

    async def test_same_domain_resolution_is_staggered(self) -> None:
        """Verify two tasks on the same host domain are staggered by at least 1.5 seconds."""
        t1 = DownloadTask(
            source_url="https://datanodes.to/abc1/file.part1.rar",
            destination=str(self.data_dir),
            display_name="file.part1.rar",
        )
        t2 = DownloadTask(
            source_url="https://datanodes.to/abc2/file.part2.rar",
            destination=str(self.data_dir),
            display_name="file.part2.rar",
        )
        self.service.store.save(t1)
        self.service.store.save(t2)

        resolve_times: list[float] = []

        def mock_resolve(source_url, *args, **kwargs):
            resolve_times.append(time.time())
            return [ResolvedItem("direct", source_url, "file", direct_url="https://direct.datanodes.to/d/1")]

        with patch.object(self.service.plugins, "resolve_chain", side_effect=mock_resolve), \
             patch.object(self.service.backend_selector, "select_for_items", side_effect=Exception("stop_after_resolve")):
            control = self.service._controls.setdefault(t1.id, type("Control", (), {"pause": type("Event", (), {"is_set": lambda self: False})(), "cancel": type("Event", (), {"is_set": lambda self: False})()})())
            # Run resolutions concurrently
            try:
                await asyncio.gather(
                    self.service._run_task_async(t1.id, {}, control, None),
                    self.service._run_task_async(t2.id, {}, control, None),
                )
            except Exception:
                pass

        self.assertEqual(len(resolve_times), 2)
        diff = abs(resolve_times[1] - resolve_times[0])
        self.assertGreaterEqual(diff, 1.45, f"Expected same-domain stagger >= 1.5s, got {diff:.2f}s")

    async def test_different_domains_resolve_concurrently(self) -> None:
        """Verify tasks on different domains do not wait for each other."""
        t1 = DownloadTask(
            source_url="https://datanodes.to/abc1/file.part1.rar",
            destination=str(self.data_dir),
            display_name="file.part1.rar",
        )
        t2 = DownloadTask(
            source_url="https://pixeldrain.com/u/xyz999",
            destination=str(self.data_dir),
            display_name="xyz999",
        )
        self.service.store.save(t1)
        self.service.store.save(t2)

        resolve_times: list[float] = []

        def mock_resolve(source_url, *args, **kwargs):
            resolve_times.append(time.time())
            return [ResolvedItem("direct", source_url, "file", direct_url="https://direct.example.com/d/1")]

        with patch.object(self.service.plugins, "resolve_chain", side_effect=mock_resolve), \
             patch.object(self.service.backend_selector, "select_for_items", side_effect=Exception("stop_after_resolve")):
            control = self.service._controls.setdefault(t1.id, type("Control", (), {"pause": type("Event", (), {"is_set": lambda self: False})(), "cancel": type("Event", (), {"is_set": lambda self: False})()})())
            try:
                await asyncio.gather(
                    self.service._run_task_async(t1.id, {}, control, None),
                    self.service._run_task_async(t2.id, {}, control, None),
                )
            except Exception:
                pass

        self.assertEqual(len(resolve_times), 2)
        diff = abs(resolve_times[1] - resolve_times[0])
        self.assertLess(diff, 0.5, f"Expected different domains to resolve concurrently, got diff {diff:.2f}s")


if __name__ == "__main__":
    unittest.main()
