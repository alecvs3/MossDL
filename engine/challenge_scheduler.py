"""Bounded, fair solver work for parked tasks.

A challenged task gives up its transfer slot at once and waits here as
`parked`; at most `limit` automatic solves run together, first come first
served (asyncio's semaphore is FIFO), so a burst of captchas on one host
cannot starve downloads or other hosts' solves.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from . import challenge_lifecycle as lifecycle
from .telemetry import telemetry_bus


class ChallengeScheduler:
    def __init__(self, limit: int = 2) -> None:
        self.limit = max(1, int(limit))
        self._slots: asyncio.Semaphore | None = None
        self.waiting = 0
        self.running = 0

    def _semaphore(self) -> asyncio.Semaphore:
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.limit)
        return self._slots

    async def run(self, challenge: Any, work: Callable[[], Awaitable[Any]]) -> Any:
        lifecycle.advance(challenge, lifecycle.PARKED, "waiting for a solver slot")
        queued_at = time.monotonic()
        self.waiting += 1
        started = False
        try:
            async with self._semaphore():
                self.waiting -= 1
                started = True
                self.running += 1
                telemetry_bus.record(level="INFO", subsystem="engine:captcha",
                                     message=f"[CHALLENGE_SLOT] {challenge.id} started after {time.monotonic() - queued_at:.1f}s",
                                     context={"challenge_id": challenge.id, "waited_seconds": round(time.monotonic() - queued_at, 2),
                                              "running": self.running, "waiting": self.waiting}, tier="engine")
                try:
                    return await work()
                finally:
                    self.running -= 1
        finally:
            if not started:
                self.waiting -= 1  # cancelled while still waiting for a slot
