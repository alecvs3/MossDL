from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .models import ResolvedItem
from .transfer_contracts import BackendCapabilities, TransferResult


class TransferBackend(Protocol):
    """Shared result contract for sync and async transfer implementations."""

    def download(
        self,
        item: ResolvedItem,
        root: str,
        progress: Callable[[int], None] | None = None,
        control=None,
    ) -> Path | Awaitable[Path]: ...

    def capabilities(self) -> BackendCapabilities: ...

    # Optional runtime probe implemented by external adapters.  Selection
    # treats adapters without this method as available for compatibility with
    # the original backend protocol.
    def available(self) -> bool: ...

    def download_result(self, item: ResolvedItem, root: str,
                        progress: Callable[[int], None] | None = None,
                        control=None) -> TransferResult | Awaitable[TransferResult]: ...
