"""Operation-scoped wait lifecycle shared by synchronous provider adapters."""
from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

from .errors import DownloadCanceled, DownloadPaused


@dataclass
class WaitOperation:
    task_id: str
    generation: str
    control: object
    notify: Callable[[dict], None]

    def is_set(self) -> bool:
        return self.control.cancel.is_set() or self.control.pause.is_set()

    def check(self) -> None:
        if self.control.cancel.is_set():
            raise DownloadCanceled()
        if self.control.pause.is_set():
            raise DownloadPaused()


operation: contextvars.ContextVar[WaitOperation | None] = contextvars.ContextVar('provider_wait', default=None)


@contextmanager
def resolution_waits(task_id: str, generation: str, control, notify):
    token = operation.set(WaitOperation(task_id, generation, control, notify))
    try:
        yield
    finally:
        operation.reset(token)


def check_control() -> None:
    current = operation.get()
    if current is not None:
        current.check()


def scoped_key(key: str) -> str:
    current = operation.get()
    return f'{current.task_id}:{current.generation}:{key}' if current else str(key)


def publish(state: str, context: dict) -> None:
    current = operation.get()
    if current is not None:
        from .critical_trace import mark
        mark(
            f"provider.wait.{state}",
            event="state",
            resource="provider_wait",
            resource_id=str(context.get("key") or context.get("host") or "provider"),
            generation=current.generation,
            **{key: value for key, value in context.items() if key not in {"task_id", "generation"}},
        )
        current.notify({**context, 'task_id': current.task_id, 'generation': current.generation,
                        'wait_state': state, 'countdown_captured_at': time.time()})
