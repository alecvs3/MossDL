"""A provider's transfer slot must be able to widen, and must not start at 1.

Two independent things serialized every DataNodes multipart package, both
upstream of the whole host-concurrency ladder:

  1. `provider_limits["datanodes"] = 1`, a static cap applied whenever the
     storage host is not yet known -- which is exactly the situation for the
     first item of a package.
  2. The slot was built with `setdefault`, so whatever width the first item
     produced was frozen for the life of the process. Later items could observe
     a higher host ceiling and still queue behind a semaphore of 1.

Together they meant no amount of ceiling probing could ever produce a second
concurrent part.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from engine.limits import ResourceManager, SchedulerPolicy, item_admission_limit


class ProviderLimitDefaultTests(unittest.TestCase):
    def test_datanodes_is_not_capped_to_one(self) -> None:
        limits = SchedulerPolicy().provider_limits
        self.assertGreater(
            limits.get("datanodes", 1), 1,
            "a static provider cap of 1 serializes every multipart package "
            "regardless of what the host ceiling learned")

    def test_other_single_connection_providers_are_untouched(self) -> None:
        """Hosts that genuinely permit one transfer keep their cap."""
        limits = SchedulerPolicy().provider_limits
        for provider in ("1fichier", "rapidgator", "ddownload"):
            self.assertEqual(limits[provider], 1, f"{provider} lost its real cap")

    def test_historical_single_stream_observation_does_not_override_static_width(self) -> None:
        self.assertEqual(item_admission_limit(4, verified_ceiling=1, breaker_limit=None), 4)

    def test_verified_success_can_widen_an_optimistic_limit(self) -> None:
        self.assertEqual(item_admission_limit(2, verified_ceiling=5, breaker_limit=None), 5)

    def test_explicit_breaker_evidence_can_narrow_the_limit(self) -> None:
        self.assertEqual(item_admission_limit(4, verified_ceiling=6, breaker_limit=1), 1)


class ProviderSlotWideningTests(unittest.IsolatedAsyncioTestCase):
    """The latch: a slot opened narrow must be able to widen."""

    def _manager(self, opening_width: int) -> ResourceManager:
        policy = SchedulerPolicy()
        policy.provider_limits["datanodes"] = opening_width
        manager = ResourceManager(policy)
        permit = MagicMock()
        permit.finish = MagicMock()
        manager.adaptive.acquire = MagicMock(return_value=permit)
        return manager

    @staticmethod
    def _item():
        return SimpleNamespace(
            provider="datanodes", direct_url="", source_url="", metadata={},
        )

    async def test_slot_widens_when_a_later_item_allows_more(self) -> None:
        manager = self._manager(1)
        first = manager.item_slot(self._item())
        await first.__aenter__()
        manager.policy.provider_limits["datanodes"] = 4
        siblings = [manager.item_slot(self._item()) for _ in range(3)]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(slot.__aenter__() for slot in siblings)),
                timeout=0.5,
            )
            self.assertEqual(manager.provider_slot_widths["datanodes"], 4)
        finally:
            await asyncio.gather(*(slot.__aexit__(None, None, None) for slot in siblings))
            await first.__aexit__(None, None, None)

    async def test_slot_never_narrows(self) -> None:
        """Shrinking mid-flight would strand permits already handed out."""
        manager = self._manager(4)
        first = manager.item_slot(self._item())
        await first.__aenter__()
        manager.policy.provider_limits["datanodes"] = 1
        second = manager.item_slot(self._item())
        try:
            await asyncio.wait_for(second.__aenter__(), timeout=0.5)
            self.assertEqual(manager.provider_slot_widths["datanodes"], 4)
        finally:
            await second.__aexit__(None, None, None)
            await first.__aexit__(None, None, None)

    async def test_resolver_request_rate_does_not_throttle_payload_admission(self) -> None:
        manager = self._manager(4)
        manager.configure_provider("datanodes", concurrency=4, requests_per_second=0.5)
        slot = manager.item_slot(self._item())
        try:
            await slot.__aenter__()
            self.assertEqual(manager.adaptive.acquire.call_args.kwargs["requests_per_second"], 0.0)
        finally:
            await slot.__aexit__(None, None, None)


class AdaptiveAdmissionWarmStartTests(unittest.TestCase):
    def test_known_item_limit_is_the_initial_admission_width(self) -> None:
        manager = ResourceManager(SchedulerPolicy())
        key = "datanodes:-:tunnel5.dlproxy.uk"

        permit = manager.adaptive.acquire(key, ceiling=4, initial_concurrency=4)
        try:
            snapshot = next(row for row in manager.adaptive.snapshot() if row["key"] == key)
            self.assertEqual(snapshot["concurrency"], 4)
            self.assertEqual(snapshot["ceiling"], 4)
        finally:
            permit.finish()

    def test_existing_reduced_window_is_not_rewidened_by_warm_start(self) -> None:
        manager = ResourceManager(SchedulerPolicy())
        key = "datanodes:-:tunnel5.dlproxy.uk"
        permit = manager.adaptive.acquire(key, ceiling=4, initial_concurrency=4)
        permit.finish(False)

        manager.adaptive.configure(key, ceiling=4, initial_concurrency=4)
        snapshot = next(row for row in manager.adaptive.snapshot() if row["key"] == key)
        self.assertEqual(snapshot["concurrency"], 2)


if __name__ == "__main__":
    unittest.main()
