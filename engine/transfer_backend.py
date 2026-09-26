from __future__ import annotations

from typing import Any, Callable, Protocol

from .models import ResolvedItem
from .transfer_contracts import BackendCapabilities, TransferResult


class TransferBackend(Protocol):
    """Common lifecycle expected by scheduler adapters and future providers."""

    def probe(self, item: ResolvedItem) -> dict[str, Any]: ...

    def start(self, item: ResolvedItem, destination: str, progress: Callable[[int], None] | None = None) -> Any: ...

    def pause(self, task_id: str) -> None: ...

    def resume(self, task_id: str) -> None: ...

    def cancel(self, task_id: str) -> None: ...

    def refresh(self, item: ResolvedItem, reason: str) -> ResolvedItem: ...

    def progress(self, task_id: str) -> dict[str, Any]: ...

    def capabilities(self) -> BackendCapabilities: ...

    def download_result(self, item: ResolvedItem, destination: str,
                        progress: Callable[[int], None] | None = None,
                        control=None) -> TransferResult: ...
