"""The concurrency ladder must be able to go UP, not only down.

`verified_ceiling` was only ever raised by observing more active streams than
the ceiling allowed -- but the ceiling is precisely what stopped those streams
from starting. A storage host calibrated to 1 stayed at 1 forever no matter how
many parts queued behind it, which is why a multipart package downloaded one
file at a time against a host that permits several.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from engine.concurrency_auditor import (
    BREAKER_CLOSED,
    BREAKER_FROZEN,
    HostConcurrencyAuditor,
    HostConcurrencyProfile,
)

HOST = "tunnel5.dlproxy.uk"


class ProbeAllowanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auditor = HostConcurrencyAuditor(Path(self.tmp.name))
        prof = HostConcurrencyProfile(host=HOST, verified_ceiling=1)
        prof.breaker.state = BREAKER_CLOSED
        self.auditor._profiles[HOST] = prof
        self.prof = prof

    def test_pinned_host_with_demand_gets_one_probe_stream(self) -> None:
        """The whole point: a healthy host pinned at 1 must be retried upward."""
        self.assertEqual(self.auditor.probe_allowance(HOST, 1, demand=3), 2)

    def test_no_demand_means_no_probe(self) -> None:
        self.assertEqual(self.auditor.probe_allowance(HOST, 1, demand=0), 1)

    def test_probe_is_rate_limited(self) -> None:
        self.assertEqual(self.auditor.probe_allowance(HOST, 1, demand=3), 2)
        self.assertEqual(
            self.auditor.probe_allowance(HOST, 1, demand=3), 1,
            "a second probe fired immediately; a host would be hammered")

    def test_probe_refused_while_breaker_is_not_closed(self) -> None:
        self.prof.breaker.state = BREAKER_FROZEN
        self.assertEqual(
            self.auditor.probe_allowance(HOST, 1, demand=3), 1,
            "probing raced the breaker's own ladder")

    def test_probe_refused_during_cooldown(self) -> None:
        self.prof.cooldown_until = time.time() + 300
        self.assertEqual(self.auditor.probe_allowance(HOST, 1, demand=3), 1)

    def test_denial_blocks_probing_while_its_cooldown_is_active(self) -> None:
        self.prof.denied_at_ceiling = 2
        self.prof.cooldown_until = time.time() + 300
        self.assertEqual(
            self.auditor.probe_allowance(HOST, 1, demand=3), 1,
            "re-probed a level the host just refused")

    def test_denial_is_a_temporary_boundary_not_a_permanent_ceiling(self) -> None:
        """A denial must expire, or one refusal pins the host forever.

        The expiry existed only inside `admission_decision`, which is gated on
        `item_concurrency > 1` and so never runs for single-item package parts.
        Denials were set and never cleared, and DataNodes stayed at one stream
        across every subsequent run.
        """
        self.prof.denied_at_ceiling = 2
        self.prof.cooldown_until = 0.0
        self.assertEqual(
            self.auditor.probe_allowance(HOST, 1, demand=3), 2,
            "a stale denial boundary permanently pinned the host at one stream")
        self.assertIsNone(self.prof.denied_at_ceiling, "the denial boundary was not retired")

    def test_probe_refused_above_the_cap(self) -> None:
        self.prof.verified_ceiling = 8
        self.assertEqual(self.auditor.probe_allowance(HOST, 8, demand=9), 8)

    def test_unknown_host_is_left_alone(self) -> None:
        self.assertEqual(self.auditor.probe_allowance("never-seen.example", 1, demand=5), 1)

    def test_probe_climbs_one_step_at_a_time(self) -> None:
        """1 -> 2 -> 3, never a jump straight to the demand count."""
        self.assertEqual(self.auditor.probe_allowance(HOST, 1, demand=6), 2)
        self.prof.verified_ceiling = 2
        self.prof.last_probe_at = 0.0
        self.assertEqual(self.auditor.probe_allowance(HOST, 2, demand=6), 3)


class ResizableSemaphoreDemandTests(unittest.TestCase):
    """The demand signal the probe depends on."""

    def test_waiters_are_counted(self) -> None:
        import asyncio

        from engine.storage_concurrency import ResizableSemaphore

        async def scenario():
            sem = ResizableSemaphore(1)
            await sem.acquire()
            self.assertEqual(sem.waiters, 0)
            blocked = [asyncio.create_task(sem.acquire()) for _ in range(3)]
            await asyncio.sleep(0.05)
            observed = sem.waiters
            sem.resize(4)
            await asyncio.gather(*blocked)
            return observed

        self.assertEqual(
            asyncio.run(scenario()), 3,
            "blocked tasks were invisible, so nothing could tell a saturated host from an idle one")

    def test_waiters_return_to_zero_after_admission(self) -> None:
        import asyncio

        from engine.storage_concurrency import ResizableSemaphore

        async def scenario():
            sem = ResizableSemaphore(1)
            await sem.acquire()
            waiter = asyncio.create_task(sem.acquire())
            await asyncio.sleep(0.05)
            await sem.release()
            await waiter
            return sem.waiters

        self.assertEqual(asyncio.run(scenario()), 0)


if __name__ == "__main__":
    unittest.main()
