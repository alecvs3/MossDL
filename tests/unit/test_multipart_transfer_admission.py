from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock

from engine.models import DownloadTask, ResolvedItem
from engine.service import EngineService


PACKAGE = "datanodes.to:game"


def _part(number: int, state: str) -> DownloadTask:
    return DownloadTask(
        id=f"part-{number}",
        source_url=f"https://datanodes.to/code/Game.part{number}.rar",
        destination="/tmp",
        display_name=f"Game.part{number}.rar",
        state=state,
        package_key=PACKAGE,
        package_part_number=number,
        resolved=[ResolvedItem(
            item_id=f"item-{number}",
            provider="datanodes",
            source_url=f"https://datanodes.to/code/Game.part{number}.rar",
            display_name=f"Game.part{number}.rar",
            direct_url=f"https://cdn.example/part{number}",
        )] if state == "preflight" else [],
    )


class MultipartTransferAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = EngineService.__new__(EngineService)
        self.service._loop = asyncio.get_running_loop()
        self.service._multipart_transfer_events = {}
        self.service._log_task = MagicMock()
        self.part1 = _part(1, "preflight")
        self.part2 = _part(2, "resolving")
        self.service.store = MagicMock()
        self.service.store.list.side_effect = lambda: [self.part1, self.part2]

    async def test_first_payload_waits_until_sibling_owns_direct_plan(self) -> None:
        waiter = asyncio.create_task(self.service._await_multipart_transfer_admission(
            self.part1, PACKAGE, "datanodes.to",
        ))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        self.part2.state = "preflight"
        self.part2.resolved = [_part(2, "preflight").resolved[0]]
        self.service._signal_multipart_transfer_events()
        await asyncio.wait_for(waiter, timeout=1)

    async def test_all_package_waiters_release_from_the_same_transition(self) -> None:
        first = asyncio.create_task(self.service._await_multipart_transfer_admission(
            self.part1, PACKAGE, "datanodes.to",
        ))
        await asyncio.sleep(0)
        self.part2.state = "preflight"
        self.part2.resolved = [_part(2, "preflight").resolved[0]]
        self.service._signal_multipart_transfer_events()
        second = asyncio.create_task(self.service._await_multipart_transfer_admission(
            self.part2, PACKAGE, "datanodes.to",
        ))
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

    async def test_failed_sibling_cannot_deadlock_ready_payload(self) -> None:
        self.part2.state = "failed"
        await asyncio.wait_for(
            self.service._await_multipart_transfer_admission(
                self.part1, PACKAGE, "datanodes.to",
            ),
            timeout=1,
        )

    async def test_other_hosts_do_not_use_datanodes_barrier(self) -> None:
        task = _part(1, "preflight")
        task.source_url = "https://example.test/Game.part1.rar"
        task.resolved[0].provider = "generic"
        self.part2.state = "resolving"
        await asyncio.wait_for(
            self.service._await_multipart_transfer_admission(
                task, PACKAGE, "example.test",
            ),
            timeout=1,
        )


if __name__ == "__main__":
    unittest.main()
