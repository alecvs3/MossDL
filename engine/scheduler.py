from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from .telemetry import telemetry_bus

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Job:
    priority: int
    sequence: int
    work: Callable[[], Awaitable[None]]
    completion: asyncio.Future[None]
    dedupe_key: str | None = None


class FairAsyncScheduler:
    """A bounded, provider-aware FIFO scheduler with round-robin fairness.

    Work is queued by fairness group (normally provider or host). The worker
    pool rotates between groups, so a large batch from one provider cannot
    monopolize all transfer slots. Priority is respected within each group.
    """

    def __init__(self, workers: int) -> None:
        self.workers = max(1, workers)
        self._groups: dict[str, list[_Job]] = {}
        self._rotation: deque[str] = deque()
        self._sequence = 0
        self._condition = asyncio.Condition()
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self._on_error: Callable[[BaseException], None] | None = None
        self._queued_keys: set[str] = set()
        self._inflight_keys: set[str] = set()

    def set_error_handler(self, handler: Callable[[BaseException], None]) -> None:
        self._on_error = handler

    async def start(self) -> None:
        if self._worker_tasks:
            return
        self._worker_tasks = [asyncio.create_task(self._worker(i), name=f"transfer-worker-{i}") for i in range(self.workers)]

    async def set_workers(self, workers: int) -> None:
        """Resize the worker pool without cancelling work already in flight."""
        self.workers = max(1, int(workers))
        self._worker_tasks = [task for task in self._worker_tasks if not task.done()]
        missing = self.workers - len(self._worker_tasks)
        start = len(self._worker_tasks)
        if missing > 0:
            self._worker_tasks.extend(
                asyncio.create_task(self._worker(start + index), name=f"transfer-worker-{start + index}")
                for index in range(missing)
            )
        async with self._condition:
            self._condition.notify_all()

    def has_job(self, dedupe_key: str | None) -> bool:
        """Check whether a job with the given dedupe key is currently queued or in-flight."""
        if not dedupe_key:
            return False
        return dedupe_key in self._queued_keys or dedupe_key in self._inflight_keys

    async def submit(
        self,
        group: str,
        work: Callable[[], Awaitable[None]],
        priority: int = 0,
        dedupe_key: str | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[None] = loop.create_future()
        group = group or "unknown"
        async with self._condition:
            if self._closed:
                raise RuntimeError("transfer scheduler is closed")
            if dedupe_key and (dedupe_key in self._queued_keys or dedupe_key in self._inflight_keys):
                self._reject_duplicate(group, dedupe_key)
                return
            self._sequence += 1
            job = _Job(priority, self._sequence, work, completion, dedupe_key=dedupe_key)
            queue = self._groups.setdefault(group, [])
            queue.append(job)
            queue.sort(key=lambda entry: (entry.priority, entry.sequence))
            if group not in self._rotation:
                self._rotation.append(group)
            if dedupe_key:
                self._queued_keys.add(dedupe_key)
            self._condition.notify()
        await completion

    @staticmethod
    def _reject_duplicate(group: str, dedupe_key: str) -> None:
        context = {"group": group, "dedupe_key": dedupe_key}
        logger.info("Rejected duplicate scheduler job group=%s dedupe_key=%s", group, dedupe_key)
        telemetry_bus.record(
            level="WARN",
            subsystem="engine:scheduler",
            message=f"[SCHEDULER_DEDUPE] Rejected duplicate job '{dedupe_key}' in group '{group}'",
            context=context,
            tier="engine",
        )

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            pending = [job for queue in self._groups.values() for job in queue]
            self._groups.clear()
            self._rotation.clear()
            self._queued_keys.clear()
            self._inflight_keys.clear()
            for job in pending:
                if not job.completion.done():
                    job.completion.set_exception(RuntimeError("transfer scheduler closed"))
            self._condition.notify_all()
        for worker in self._worker_tasks:
            worker.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()

    async def _next_job(self) -> _Job | None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._closed or bool(self._rotation))
            if self._closed and not self._rotation:
                return None
            group = self._rotation.popleft()
            queue = self._groups[group]
            job = queue.pop(0)
            if queue:
                self._rotation.append(group)
            else:
                del self._groups[group]
            if job.dedupe_key:
                self._queued_keys.discard(job.dedupe_key)
                self._inflight_keys.add(job.dedupe_key)
            return job

    async def _worker(self, worker_index: int) -> None:
        while True:
            if worker_index >= self.workers:
                return
            job = await self._next_job()
            if job is None:
                return
            try:
                # Each job runs as its own Task, so it gets its own copy of the
                # context: state a job binds for itself (its route, for one)
                # must not carry into the next job this worker picks up.
                # Cancelling the worker cancels the job it is awaiting.
                await asyncio.create_task(job.work())
            except asyncio.CancelledError:
                if not job.completion.done():
                    job.completion.cancel()
                raise
            except BaseException as exc:
                if not job.completion.done():
                    job.completion.set_exception(exc)
                if self._on_error is not None:
                    try:
                        self._on_error(exc)
                    except BaseException:
                        pass
            else:
                if not job.completion.done():
                    job.completion.set_result(None)
            finally:
                if job.dedupe_key:
                    async with self._condition:
                        self._inflight_keys.discard(job.dedupe_key)
