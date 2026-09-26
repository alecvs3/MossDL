from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from engine.limits import ResourceManager, SchedulerPolicy
from engine.models import DownloadTask, ResolvedItem
from engine.service import EngineService, _TaskControl


class _ConcurrentRustBackend:
    """Synchronous backend double exercising EngineService's real adapter path."""

    def __init__(self, expected: int) -> None:
        self.barrier = threading.Barrier(expected, timeout=3)
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.segment_budgets: list[int | None] = []
        self.last_segments = 1

    def download(self, item, destination, _progress, _control, route_profile=None):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.segment_budgets.append((item.metadata or {}).get("max_segments"))
        try:
            self.barrier.wait()
            return Path(destination) / item.display_name
        finally:
            with self.lock:
                self.active -= 1


class MainEngineDataNodesAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_package_parts_reach_rust_together_with_one_segment_each(self) -> None:
        service = EngineService.__new__(EngineService)
        policy = SchedulerPolicy(per_host_transfers=4, max_active_segments=8)
        policy.provider_limits["datanodes"] = 4
        service.resources = ResourceManager(policy)
        service.rust_backend = _ConcurrentRustBackend(expected=4)

        with tempfile.TemporaryDirectory() as destination:
            calls = []
            items = []
            for number in range(1, 5):
                name = f"Game.part{number}.rar"
                task = DownloadTask(
                    id=f"part-{number}",
                    source_url=f"https://datanodes.to/code-{number}/{name}",
                    destination=destination,
                    display_name=name,
                    provider="datanodes",
                    package_key="datanodes.to:game",
                    package_part_number=number,
                )
                item = ResolvedItem(
                    provider="datanodes",
                    source_url=task.source_url,
                    display_name=name,
                    direct_url=f"https://integration-cdn.invalid/{name}",
                    metadata={"type": "file"},
                )
                items.append(item)
                calls.append(service._download_item_with_refresh(
                    task,
                    0,
                    item,
                    destination,
                    lambda _count: None,
                    _TaskControl(),
                    {},
                    "rust",
                    {},
                ))

            await asyncio.wait_for(asyncio.gather(*calls), timeout=5)

        self.assertEqual(service.rust_backend.peak, 4)
        self.assertEqual(sorted(service.rust_backend.segment_budgets), [1, 1, 1, 1])
        self.assertTrue(all(item.metadata["segment_budget_reason"] == "multipart_file_parallelism"
                            for item in items))
        self.assertEqual(service.resources.provider_slot_widths["datanodes"], 4)
        host_window = service.resources.hosts["integration-cdn.invalid"]
        self.assertEqual(host_window.effective_window(), 4)


if __name__ == "__main__":
    unittest.main()
