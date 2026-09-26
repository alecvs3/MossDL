"""Shared transfer request/result contracts.

Backends keep their existing ``download() -> Path`` compatibility surface, but
the engine can use these contracts to normalize reporting and verification.
URLs and request material are intentionally operation-scoped values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .reliability import DownloadIntegrityReport, RetryPolicy


@dataclass(slots=True)
class BackendCapabilities:
    name: str
    supports_resume: bool = True
    supports_ranges: bool = True
    supports_dynamic_ranges: bool = True
    supports_pause: bool = True
    supports_cancel: bool = True
    supports_postprocess: bool = False
    supports_streaming_transform: bool = False
    supports_media_segments: bool = False
    supports_refresh: bool = True
    supports_integrity: bool = True
    supports_operation_scoped_headers: bool = True
    available: bool = True
    availability_reason: str | None = None
    supported_route_kinds: tuple[str, ...] = ("direct",)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "supports_resume": self.supports_resume,
            "supports_ranges": self.supports_ranges,
            "supports_dynamic_ranges": self.supports_dynamic_ranges,
            "supports_pause": self.supports_pause,
            "supports_cancel": self.supports_cancel,
            "supports_postprocess": self.supports_postprocess,
            "supports_streaming_transform": self.supports_streaming_transform,
            "supports_media_segments": self.supports_media_segments,
            "supports_refresh": self.supports_refresh,
            "supports_integrity": self.supports_integrity,
            "supports_operation_scoped_headers": self.supports_operation_scoped_headers,
            "available": self.available,
            "availability_reason": self.availability_reason,
            "supported_route_kinds": list(self.supported_route_kinds),
        }


@dataclass(slots=True)
class TransferRequest:
    source_url: str
    destination_root: str | Path
    relative_path: str
    expected_size: int | None = None
    expected_checksum: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    referrer: str | None = None
    account_ref: str | None = None
    backend: str = "custom"
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    progress: Callable[[int], None] | None = None


@dataclass(slots=True)
class TransferResult:
    path: str
    bytes_written: int
    backend: str
    resumed: bool = False
    attempts: int = 1
    integrity: DownloadIntegrityReport | None = None
    verification_state: str = "unverifiable"
    failure: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "bytes_written": self.bytes_written,
            "backend": self.backend,
            "resumed": self.resumed,
            "attempts": self.attempts,
            "integrity": self.integrity.to_dict() if self.integrity else None,
            "verification_state": self.verification_state,
            "failure": self.failure,
            "metadata": self.metadata,
        }


def result_from_path(path: str | Path, backend: str, *, resumed: bool = False,
                     attempts: int = 1, integrity: DownloadIntegrityReport | None = None,
                     metadata: dict[str, Any] | None = None) -> TransferResult:
    target = Path(path)
    return TransferResult(
        path=str(target),
        bytes_written=target.stat().st_size if target.is_file() else 0,
        backend=backend,
        resumed=resumed,
        attempts=max(1, int(attempts)),
        integrity=integrity,
        verification_state=integrity.state if integrity else "unverifiable",
        metadata=dict(metadata or {}),
    )
