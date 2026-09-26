from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .models import ResolvedItem
from .errors import DownloadCanceled, DownloadPaused
from .reliability import TransportSignal, TransportSignalError, verify_file
from .telemetry import telemetry_bus
from . import rust_session
from .rust_session import find_transfer_core
from . import route_http
from .route_http import proxy_url_for
from .transfer_contracts import BackendCapabilities, TransferResult, result_from_path

_CORE_ERROR_PATTERN = re.compile(r"^(THROTTLED|RETRYABLE|AUTH):(\d{3}):(\d+(?:\.\d+)?)")
_CORE_ERROR_CATEGORIES = {"THROTTLED": "throttle", "RETRYABLE": "retryable", "AUTH": "auth"}

# Routes the kernel handles for us, where the core connects directly.
ROUTE_KINDS = ("direct", "system_vpn", "wireguard", "http_proxy", "socks5", "docker_socks5")

# Provider transforms the core can apply while the bytes are in flight.
SUPPORTED_TRANSFORMS = frozenset({"mega-ctr"})


def signal_from_core_error(message: str) -> TransportSignal | None:
    """Parse a structured transfer-core error string into a transport signal.

    The Rust core emits ``THROTTLED|RETRYABLE|AUTH:<status>:<retry_after>``
    followed by optional human-readable context. Unknown messages return None
    so callers keep their existing error handling.
    """
    match = _CORE_ERROR_PATTERN.match(str(message or "").strip())
    if not match:
        return None
    kind, status_text, retry_text = match.groups()
    retry_after = float(retry_text)
    return TransportSignal(
        category=_CORE_ERROR_CATEGORIES[kind],
        retry_after_seconds=retry_after if retry_after > 0 else None,
        status_code=int(status_text),
        evidence=str(message).strip()[:300],
    )



class RustTransferBackend:
    def __init__(self, max_segments: int = 16, min_segment_size: int = 8 * 1024 * 1024, max_retries: int = 4,
                 bandwidth_rate: int = 0) -> None:
        self.max_segments = max_segments
        self.min_segment_size = min_segment_size
        self.max_retries = max_retries
        self.bandwidth_rate = max(0, int(bandwidth_rate))
        # Ranges used by the most recent transfer, as reported by transfer-core.
        self.last_segments = 1

    def available(self) -> bool:
        return find_transfer_core() is not None

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            "rust",
            supports_ranges=True,
            supports_dynamic_ranges=True,
            supports_postprocess=True,
            supports_streaming_transform=False,
            supports_media_segments=False,
            supports_refresh=True,
            available=self.available(),
            availability_reason=None if self.available() else "Rust transfer-core binary is unavailable",
            supported_route_kinds=ROUTE_KINDS,
        )

    @staticmethod
    def route_proxy(route_profile: dict | None) -> str | None:
        """The proxy URL a route profile implies, or None for kernel-routed routes."""
        return proxy_url_for(route_profile)

    @staticmethod
    def _request_headers(item: ResolvedItem) -> dict[str, str]:
        """Build the wire headers every in-process backend already preserves."""
        headers = dict(item.headers or {})
        if item.cookies and not any(str(key).lower() == "cookie" for key in headers):
            headers["Cookie"] = "; ".join(
                f"{key}={value}" for key, value in item.cookies.items()
            )
        return headers

    def download_result(self, item, root, progress=None, control=None, max_segments: int | None = None,
                        route_profile: dict | None = None) -> TransferResult:
        relative_target = item.relative_path or item.display_name
        if item.relative_path and item.display_name and Path(item.relative_path).name != item.display_name:
            relative_target = str(Path(item.relative_path) / item.display_name)
        destination = Path(root).resolve() / relative_target
        resumed = destination.with_name(destination.name + ".part").exists()
        path = self.download(item, root, progress, control, max_segments=max_segments,
                             route_profile=route_profile)
        algorithm, _, expected = item.checksum.partition(":") if item.checksum else ("sha256", "", None)
        report = verify_file(path, expected_size=item.size, expected_checksum=expected,
                             algorithm=algorithm or "sha256",
                             content_type=(item.metadata or {}).get("content_type"))
        return result_from_path(path, "rust", resumed=resumed, integrity=report)

    def download(self, item: ResolvedItem, root: str | Path, progress: Callable[[int], None] | None = None,
                 control=None, max_segments: int | None = None, route_profile: dict | None = None) -> Path:
        if control and control.cancel.is_set():
            raise RuntimeError("canceled before Rust transfer started")
        transform = dict(item.postprocess or {})
        if transform and transform.get("type") not in SUPPORTED_TRANSFORMS:
            # Fail before starting rather than part way through a large file;
            # the engine's fallback then hands this to the Python transport.
            raise RuntimeError(f"Rust transfer-core cannot apply the {transform.get('type')!r} transform")

        root_path = Path(root).resolve()
        relative_target = item.relative_path or item.display_name
        if item.relative_path and item.display_name and Path(item.relative_path).name != item.display_name:
            relative_target = str(Path(item.relative_path) / item.display_name)
        destination = root_path / relative_target
        destination.parent.mkdir(parents=True, exist_ok=True)

        strategy = str((item.metadata or {}).get("duplicate_strategy", "skip"))
        if destination.exists() and item.size is not None and destination.stat().st_size == item.size:
            if strategy == "skip":
                if progress:
                    progress(item.size)
                return destination
            if strategy == "rename":
                stem, suffix = destination.stem, destination.suffix
                counter = 1
                while True:
                    candidate = destination.with_name(f"{stem} ({counter}){suffix}")
                    if not candidate.exists():
                        destination = candidate
                        relative_target = str(destination.relative_to(root_path))
                        break
                    counter += 1
            elif strategy == "overwrite":
                try:
                    destination.unlink()
                except OSError:
                    pass

        request_headers = self._request_headers(item)

        # If item.size is None, probe Content-Length upfront via HEAD/Range preflight
        resolved_size = item.size
        if resolved_size is None and item.direct_url:
            try:
                import urllib.request
                req = urllib.request.Request(item.direct_url, headers={**request_headers, "Range": "bytes=0-0"})
                # The task's own route: this preflight is part of the transfer.
                with route_http.urlopen(req, timeout=5, proxy=proxy_url_for(route_profile)) as resp:
                    from .challenge_classifier import observe_received_response
                    observe_received_response(
                        source="rust_backend.range_preflight", status=getattr(resp, "status", 200),
                        headers=dict(resp.headers), final_url=resp.geturl() or item.direct_url,
                    )
                    cr = resp.headers.get("Content-Range") or ""
                    if "/" in cr:
                        total_part = cr.rsplit("/", 1)[-1]
                        if total_part.isdigit():
                            resolved_size = int(total_part)
                    if resolved_size is None:
                        cl = resp.headers.get("Content-Length")
                        if cl and cl.isdigit():
                            resolved_size = int(cl)
            except Exception as exc:
                # The core probes again itself; record why this one gave nothing.
                telemetry_bus.record(
                    level="DEBUG", subsystem="engine:rust_backend",
                    message=f"[RANGE_PREFLIGHT] no size from preflight: {type(exc).__name__}: {exc}",
                    context={"url": item.direct_url}, tier="engine",
                )

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "download",
            "params": {
                "url": item.direct_url,
                "root": str(root_path),
                "relative_path": relative_target,
                "headers": request_headers,
                "size": resolved_size,
                "checksum": item.checksum,
                "max_segments": (item.metadata or {}).get("max_segments") or max_segments or self.max_segments,
                "min_segment_size": self.min_segment_size,
                "max_retries": self.max_retries,
                "bandwidth_bytes_per_second": self.bandwidth_rate,
                "proxy": self.route_proxy(route_profile),
                "transform": transform or None,
            },
        }
        corrected_total_logged = False
        transfer_id, inbox = rust_session.session().start_download(payload["params"])

        def _note_total(total: int) -> None:
            nonlocal corrected_total_logged
            if total > 0 and item.size != total:
                if not corrected_total_logged:
                    telemetry_bus.record(
                        level="INFO",
                        subsystem="engine:transport",
                        message=(f"[REMOTE_SIZE_CORRECTED] {item.display_name}: "
                                 f"transport probe established {total} bytes"),
                        context={"declared_bytes": item.size, "actual_bytes": total},
                        tier="engine",
                    )
                    corrected_total_logged = True
                item.size = total

        try:
            response = rust_session.wait_for_result(
                inbox,
                progress=progress,
                on_total=_note_total,
                should_pause=lambda: bool(control and control.pause.is_set()),
                should_cancel=lambda: bool(control and control.cancel.is_set()),
                request_stop=lambda cancel: rust_session.session().stop(transfer_id, cancel=cancel),
            )
        finally:
            rust_session.session().finish(transfer_id)

        if not response:
            raise RuntimeError(rust_session.session().stderr_tail().strip()
                               or "Rust transfer-core returned no response")
        error = response.get("error") or {}
        # Stopping is the engine's own instruction coming back, not a failure.
        if error.get("code") == "PAUSED":
            raise DownloadPaused("transfer paused")
        if error.get("code") == "CANCELED":
            raise DownloadCanceled("transfer canceled")
        if response.get("error"):
            message = response["error"].get("message", "Rust transfer failed")
            evidence = response["error"].get("response_evidence") or {}
            if isinstance(evidence, dict):
                from .challenge_classifier import observe_received_response
                observe_received_response(
                    source="rust_transfer.error", status=evidence.get("status"),
                    headers=evidence.get("headers") if isinstance(evidence.get("headers"), dict) else {},
                    final_url=str(evidence.get("final_url") or item.direct_url or ""),
                    body_prefix=str(evidence.get("body_prefix") or "").encode("utf-8")[:65536],
                )
            signal = signal_from_core_error(message)
            if signal is not None:
                raise TransportSignalError(signal, message)
            raise RuntimeError(message)
        result = response.get("result") or {}
        path = Path(result.get("path", ""))
        if not path.is_file():
            raise RuntimeError("Rust transfer-core reported a missing output")
        actual_bytes = int(result.get("bytes", path.stat().st_size if path.exists() else 0))
        if item.size != actual_bytes:
            telemetry_bus.record(
                level="INFO",
                subsystem="engine:transport",
                message=f"[REMOTE_SIZE_CORRECTED] {item.display_name}: transport probe established {actual_bytes} bytes",
                context={"declared_bytes": item.size, "actual_bytes": actual_bytes},
                tier="engine",
            )
            item.size = actual_bytes
        # Record how many ranges the core ACTUALLY used. This was discarded, so
        # nothing downstream could tell a segmented transfer from a single
        # connection -- and the download log's "streams_allocated" reported the
        # resolved-item count instead, which is always 1 for a package part. A
        # run that fluctuated between 80 MB/s and near-zero was indistinguishable
        # from a healthy one.
        try:
            self.last_segments = max(1, int(result.get("segments") or 1))
        except (TypeError, ValueError):
            self.last_segments = 1
        telemetry_bus.record(
            level="DEBUG",
            subsystem="engine:transport",
            message=(
                f"[RUST_TRANSFER_SEGMENTS] {item.display_name or path.name}: "
                f"{self.last_segments} range(s), {actual_bytes} bytes"
            ),
            context={"segments": self.last_segments, "bytes": actual_bytes,
                     "requested_max_segments": payload["params"].get("max_segments")},
            tier="engine",
        )
        if progress:
            progress(actual_bytes)
        return path
