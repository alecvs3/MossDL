from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .errors import DownloadCanceled, DownloadPaused, NeedsUser
from .limits import CircuitOpenError, ResourceManager
from .models import ResolvedItem
from .segment_stealer import DynamicSegmentCoordinator, SegmentState
from .range_planner import RangeCapability, RangePlanner
from .transport_pool import PooledTransportManager
from . import route_http
from .route_http import proxy_url_for
from .reliability import (
    TransportSignal,
    TransportSignalError,
    classify_transport_signal,
    decorrelated_jitter,
    verify_file,
)
from .transfer_contracts import BackendCapabilities, TransferResult, result_from_path
from .resume_identity import content_identity

logger = logging.getLogger(__name__)

TRANSPORT_RETRY_CATEGORIES = frozenset({"throttle", "retryable", "auth"})


class RangeUnsupported(RuntimeError):
    pass


class RetryAfterError(RuntimeError):
    def __init__(self, delay: float, message: str) -> None:
        super().__init__(message)
        self.delay = max(0.0, delay)


class TransportRetryError(RetryAfterError):
    """A classified retryable transport response (throttle/transient/auth)."""

    def __init__(self, signal: TransportSignal, message: str | None = None) -> None:
        super().__init__(signal.retry_after_seconds or 0.0,
                         message or signal.evidence or f"HTTP {signal.status_code} {signal.category}")
        self.transport_signal = signal
        self.category = signal.category
        self.status_code = signal.status_code
        self.retry_after = signal.retry_after_seconds


class _HttpxResponse:
    """Small response adapter matching the stdlib response methods we use."""

    def __init__(self, client, response) -> None:
        self._client = client
        self._response = response
        self.status = response.status_code
        self.headers = response.headers
        self._chunks = response.iter_bytes(chunk_size=1024 * 1024)

    def read(self, _size: int = -1) -> bytes:
        if _size == -1:
            return self._response.read()
        try:
            return next(self._chunks)
        except StopIteration:
            return b""

    def close(self) -> None:
        self._response.close()
        self._client.close()


def _sanitize_path_component(name: str) -> str:
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', name).strip('. ')
    base = cleaned.split('.')[0].upper()
    if base in {'CON', 'PRN', 'AUX', 'NUL'} or re.match(r'^(COM|LPT)[1-9]$', base):
        cleaned = f'_{cleaned}'
    return cleaned or 'file'


def _sanitize_relative(relative: str) -> str:
    parts = [p for p in re.split(r'[\\/]+', relative) if p and p not in ('.', '..')]
    if not parts:
        return 'unnamed_file'
    return '/'.join(_sanitize_path_component(p) for p in parts)


def _safe_path(root: Path, relative: str) -> Path:
    clean_rel = _sanitize_relative(relative)
    candidate = (root / clean_rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("download path escapes destination directory")
    return candidate


def _replace_verified(source: Path, destination: Path) -> None:
    """Atomically publish a verified file, including when a destination exists."""
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise RuntimeError(f"could not atomically replace verified destination: {destination}") from exc


FSCTL_SET_SPARSE = 0x000900C4


def _enable_sparse_file(file_or_fd) -> bool:
    """Enable sparse file allocation on Windows using DeviceIoControl with FSCTL_SET_SPARSE.

    Bypasses NTFS synchronous zero-filling (Valid Data Length trap) upon truncation.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        fd = file_or_fd if isinstance(file_or_fd, int) else file_or_fd.fileno()
        handle = msvcrt.get_osfhandle(fd)
        bytes_returned = wintypes.DWORD(0)
        res = ctypes.windll.kernel32.DeviceIoControl(
            handle,
            FSCTL_SET_SPARSE,
            None,
            0,
            None,
            0,
            ctypes.byref(bytes_returned),
            None,
        )
        return bool(res)
    except Exception:
        return False


def _allocate_sparse_file(handle, size: int) -> None:
    """Allocate space for a sparse file without triggering MSVCRT synchronous zero-filling."""
    _enable_sparse_file(handle)
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            import msvcrt

            os_handle = msvcrt.get_osfhandle(handle.fileno())
            li_distance = wintypes.LARGE_INTEGER(size)
            li_new = wintypes.LARGE_INTEGER(0)
            file_begin = 0
            if ctypes.windll.kernel32.SetFilePointerEx(os_handle, li_distance, ctypes.byref(li_new), file_begin):
                if ctypes.windll.kernel32.SetEndOfFile(os_handle):
                    ctypes.windll.kernel32.SetFilePointerEx(os_handle, wintypes.LARGE_INTEGER(0), None, file_begin)
                    return
        except Exception:
            pass
    handle.truncate(size)



class CustomAsyncBackend:
    """Provider-aware segmented downloader kept as the break-glass transport.

    Network calls use blocking stdlib HTTP in worker threads so the project has
    no mandatory native Python HTTP dependency. Admission, cancellation,
    fairness, throttling, retries, and progress remain asyncio-native.
    """

    def __init__(
        self,
        resources: ResourceManager,
        transport_pool: PooledTransportManager | None = None,
        storage_concurrency: Any | None = None,
    ) -> None:
        self.resources = resources
        self.transport_pool = transport_pool or PooledTransportManager()
        self.storage_concurrency = storage_concurrency

    def _get_host_concurrency_limit(self, host: str) -> int:
        clean_host = (host or "").lower().strip()
        if not clean_host:
            return self.resources.policy.per_host_transfers

        limit = self.resources.policy.per_host_transfers

        # 1. Check StorageHostConcurrencyManager if attached
        if getattr(self, "storage_concurrency", None) is not None:
            try:
                st_lim = self.storage_concurrency.get_limit(clean_host)
                if st_lim:
                    limit = min(limit, int(st_lim))
            except Exception:
                pass

        # 2. Check HostConcurrencyAuditor profiles & breaker
        try:
            from .concurrency_auditor import concurrency_auditor
            prof = concurrency_auditor.get_profile(clean_host)
            if prof:
                in_cd, _, _ = concurrency_auditor.is_host_in_cooldown(clean_host)
                if in_cd or (prof.denied_at_ceiling is not None and prof.verified_ceiling <= 1):
                    limit = min(limit, 1)
                elif prof.denied_at_ceiling is not None:
                    limit = min(limit, prof.verified_ceiling)
            breaker_limit = concurrency_auditor.breaker_admission_limit(clean_host)
            if breaker_limit is not None:
                limit = min(limit, breaker_limit)
        except Exception:
            pass

        return max(1, limit)

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            "custom",
            supports_ranges=True,
            supports_dynamic_ranges=True,
            supports_postprocess=True,
            supports_streaming_transform=True,
            supports_media_segments=True,
            supports_refresh=True,
            supported_route_kinds=("direct", "http_proxy", "socks5", "system_vpn", "docker_socks5", "wireguard"),
        )

    async def download_result(self, item, root, progress=None, control=None, route_profile=None) -> TransferResult:
        rel = item.relative_path or item.display_name
        if item.relative_path and item.display_name and Path(item.relative_path).name != item.display_name:
            rel = str(Path(item.relative_path) / item.display_name)
        part = _safe_path(Path(root).resolve(), rel)
        resumed = part.with_name(part.name + ".part").exists()
        path = await self.download(item, root, progress, control, route_profile)
        checksum = item.checksum.partition(":") if item.checksum else ("sha256", "", None)
        report = verify_file(
            path,
            expected_size=item.size,
            expected_checksum=checksum[2],
            algorithm=checksum[0] or "sha256",
            content_type=(item.metadata or {}).get("content_type"),
            etag=(item.metadata or {}).get("etag"),
            last_modified=(item.metadata or {}).get("last_modified"),
        )
        return result_from_path(path, "custom", resumed=resumed, integrity=report)

    async def download(
        self,
        item: ResolvedItem,
        root: str | Path,
        progress: Callable[[int], None] | None = None,
        control=None,
        route_profile: dict | None = None,
    ) -> Path:
        if not item.direct_url:
            raise ValueError("resolved item has no direct URL")
        rel = item.relative_path or item.display_name
        if item.relative_path and item.display_name and Path(item.relative_path).name != item.display_name:
            rel = str(Path(item.relative_path) / item.display_name)
        destination = _safe_path(Path(root).resolve(), rel)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if item.size is not None and item.size > self.resources.policy.max_file_size:
            raise ValueError(f"file exceeds configured limit ({item.size} bytes)")
        strategy = str((item.metadata or {}).get("duplicate_strategy", "skip"))
        if destination.exists() and item.size is not None and destination.stat().st_size == item.size:
            if strategy == "skip":
                if progress:
                    progress(item.size)
                return destination
            if strategy == "prompt":
                raise NeedsUser("A file with the same name already exists", "duplicate",
                                {"action": "duplicate", "path": str(destination), "strategies": ["skip", "overwrite", "rename"]})
            if strategy == "rename":
                destination = self._unique_destination(destination)
            elif strategy == "overwrite":
                destination.unlink()
            else:
                raise ValueError("duplicate_strategy must be skip, overwrite, rename, or prompt")

        async with self.resources.active_tasks:
            async with self.resources.item_slot(item):
                loop = asyncio.get_running_loop()
                if item.postprocess and (item.postprocess or {}).get("type") == "mega-ctr":
                    # MEGA CTR has no block chaining: each byte can be
                    # decrypted independently when the counter is advanced to
                    # the encrypted range offset. Keep the range coordinator
                    # and decrypt each wire chunk at its absolute offset.
                    return await self._download_segmented(
                        item, destination, progress, control, loop, route_profile,
                        transform=item.postprocess,
                    )
                if item.postprocess:
                    await self.resources.acquire_request(item.direct_url)
                    target = destination.with_name(destination.name + ".encrypted")
                    await asyncio.to_thread(self._download_single, item, target, progress, control, loop, route_profile)
                    await asyncio.to_thread(self._postprocess, target, destination, item.postprocess)
                    return destination
                return await self._download_segmented(item, destination, progress, control, loop, route_profile)

    async def _download_segmented(self, item, destination, progress, control, loop, route_profile=None, transform=None) -> Path:
        await self.resources.acquire_request(item.direct_url)
        metadata = await asyncio.to_thread(self._probe, item, route_profile)
        host = (urlsplit(item.direct_url or item.source_url or "").hostname or "").lower()
        host_limit = self._get_host_concurrency_limit(host)
        planner = RangePlanner(
            min_range_size=max(1024, int(self.resources.policy.min_segment_size)),
            max_range_size=max(
                max(1024, int(self.resources.policy.min_segment_size)),
                min(64 * 1024 * 1024, max(1024, int(self.resources.policy.min_segment_size)) * 8),
            ),
            initial_concurrency=max(1, min(int(self.resources.policy.initial_segment_concurrency), host_limit)),
            max_concurrency=max(1, min(
                int(self.resources.policy.max_segments_per_file),
                int(self.resources.policy.per_host_transfers),
                int(self.resources.policy.max_active_segments),
                host_limit,
            )),
        )
        expected_validator = (item.metadata or {}).get("etag") or (item.metadata or {}).get("last_modified")
        classification = planner.probe_and_classify(
            metadata, expected_size=item.size, expected_validator=expected_validator,
        )
        # A changed validator or an unsatisfiable probe can be transient on a
        # rotating CDN. Re-probe exactly once, then downgrade safely.
        if classification.capability in {RangeCapability.VALIDATOR_CONFLICT, RangeCapability.INVALID_416}:
            reprobe = await asyncio.to_thread(self._probe, item, route_profile)
            classification = planner.probe_and_classify(
                reprobe, expected_size=item.size, expected_validator=expected_validator,
            )
            metadata = reprobe
        # Keep validators in the operation-scoped item metadata so a resumed
        # request can use If-Range without making them part of task identity.
        item.metadata = {**(item.metadata or {}), **{
            key: classification.validator if key == "validator" else metadata[key]
            for key in ("etag", "last_modified", "content_type", "validator")
            if metadata.get(key) or (key == "validator" and classification.validator)
        }}
        size = item.size if item.size is not None else classification.size
        if size is not None and item.size is None:
            item.size = int(size)
        if size is not None:
            self._check_disk(destination, int(size))
        supports_ranges = classification.supports_ranges and size is not None and size > planner.min_range_size
        if not supports_ranges:
            if transform:
                target = destination.with_name(destination.name + ".encrypted")
                await self._download_single_with_retries(item, target, progress, control, loop, route_profile)
                await asyncio.to_thread(self._postprocess, target, destination, transform)
                return destination
            return await self._download_single_with_retries(item, destination, progress, control, loop, route_profile)

        range_plan = planner.plan_initial_ranges(
            int(size), capability=classification.capability, validator=classification.validator,
        )
        part = destination.with_name(destination.name + ".part")
        manifest_path = part.with_name(part.name + ".segments.json")
        
        coordinator = DynamicSegmentCoordinator(
            manifest_path=manifest_path,
            total_size=size,
            min_steal_bytes=max(1024 * 1024, self.resources.policy.min_segment_size // 4),
            data_path=part,
            identity=content_identity(item, classification.validator),
        )
        coordinator.load_or_init(range_plan.initial_ranges)

        if not part.exists() or part.stat().st_size != size:
            mode = "r+b" if part.exists() else "wb"
            try:
                with part.open(mode) as handle:
                    _allocate_sparse_file(handle, size)
            except OSError:
                with part.open("wb") as handle:
                    _allocate_sparse_file(handle, size)

        total = coordinator.total_bytes_done()
        progress_lock = asyncio.Lock()
        last_report_time = time.monotonic()

        async def report(delta: int) -> None:
            nonlocal total, last_report_time
            async with progress_lock:
                total += delta
                if progress:
                    now_m = time.monotonic()
                    if now_m - last_report_time >= 0.250 or (size and total >= size):
                        progress(total)
                        last_report_time = now_m

        worker_concurrency = max(1, min(
            range_plan.max_concurrency,
            self.resources.policy.max_segments_per_file,
            self.resources.policy.per_host_transfers,
            self.resources.policy.max_active_segments,
            host_limit,
        ))
        segment_host = await self.resources.prepare_segment_window(
            item.direct_url,
            min(range_plan.initial_concurrency, worker_concurrency),
        )

        async def worker_loop(worker_idx: int) -> None:
            worker_id = f"worker-{worker_idx}"
            while not coordinator.is_all_done():
                self._check_control(control)
                seg = coordinator.claim_work(worker_id)
                if not seg:
                    # Extend only the unplanned suffix. append_range is atomic,
                    # so competing workers cannot create overlap or a gap.
                    future = planner.next_range(
                        coordinator.next_unplanned_offset(), int(size),
                        observed_bytes_per_second=planner.observed_rate(),
                    )
                    if future and coordinator.append_range(*future):
                        continue
                    # No work left or could not steal; wait briefly or exit.
                    await asyncio.sleep(0.05)
                    if coordinator.is_all_done():
                        break
                    # Double check if any pending remains
                    if not any(s.status in {"pending", "active"} for s in coordinator.segments):
                        break
                    continue

                seg_id = seg.id
                attempt = 0
                last_backoff = 0.0
                while seg.done < seg.expected:
                    self._check_control(control)
                    host = segment_host
                    await host.acquire()
                    try:
                        await self.resources.active_segments.acquire()
                    except BaseException:
                        await host.release(False)
                        raise
                    success = False
                    circuit = None
                    sleep_for: float | None = None
                    try:
                        circuit = await self.resources.circuit_for(item.direct_url)
                        await circuit.before()
                        remaining_start = seg.start + seg.done
                        remaining_end = seg.end
                        await self.resources.acquire_request(item.direct_url)

                        # Fetch range chunk by chunk with dynamic end-boundary check
                        fetch_args = [
                            item,
                            part,
                            seg,
                            remaining_start,
                            remaining_end,
                            control,
                            loop,
                            route_profile,
                            coordinator,
                            report,
                        ]
                        if transform is not None:
                            fetch_args.append(transform)
                        started = time.monotonic()
                        written = await asyncio.to_thread(self._fetch_range_stealer, *fetch_args)
                        planner.observe(written, time.monotonic() - started)
                        success = seg.status == "completed" or seg.done >= seg.expected
                        await circuit.record(True)
                        attempt = 0
                        break  # Finished this segment piece or was split
                    except (DownloadPaused, DownloadCanceled):
                        coordinator.release_segment(seg_id)
                        raise
                    except RangeUnsupported:
                        coordinator.release_segment(seg_id)
                        if circuit is not None:
                            await circuit.record(True)
                        raise
                    except TransportSignalError:
                        # Terminal transport classification: never retry and
                        # never fall back into the part-deleting range path.
                        coordinator.release_segment(seg_id)
                        if circuit is not None:
                            await circuit.record(False)
                        raise
                    except RetryAfterError as exc:
                        attempt += 1
                        if attempt > self.resources.policy.max_retries:
                            coordinator.release_segment(seg_id)
                            raise
                        last_backoff = self._backoff_delay(exc, last_backoff)
                        sleep_for = last_backoff
                    except CircuitOpenError:
                        attempt += 1
                        if attempt > self.resources.policy.max_retries:
                            coordinator.release_segment(seg_id)
                            raise
                        last_backoff = decorrelated_jitter(last_backoff, base_seconds=1.0, max_seconds=15.0)
                        sleep_for = last_backoff
                    except (OSError, ValueError, TypeError):
                        attempt += 1
                        if attempt > self.resources.policy.max_retries:
                            coordinator.release_segment(seg_id)
                            raise
                        last_backoff = decorrelated_jitter(last_backoff, base_seconds=1.0, max_seconds=30.0)
                        sleep_for = last_backoff
                    except Exception:
                        if circuit is not None:
                            await circuit.record(False)
                        attempt += 1
                        if attempt > self.resources.policy.max_retries:
                            coordinator.release_segment(seg_id)
                            raise
                        last_backoff = decorrelated_jitter(last_backoff, base_seconds=1.0, max_seconds=30.0)
                        sleep_for = last_backoff
                    finally:
                        await host.release(success)
                        self.resources.active_segments.release()
                    if sleep_for is not None:
                        # Admission permits are already released, so a throttled
                        # worker cannot pin host/segment slots during backoff.
                        await asyncio.sleep(sleep_for)

        range_failure = False
        fallback_reason: str | None = None
        try:
            async with asyncio.TaskGroup() as group:
                for idx in range(worker_concurrency):
                    group.create_task(worker_loop(idx))
        except ExceptionGroup as errors:
            # Unwrap transport signals BEFORE deciding this is a range problem.
            # Re-raising the ExceptionGroup discards the signal's status_code and
            # retry_after, so the caller classified an ordinary 403/429 as an
            # `unknown` failure with `status=None`, declined to retry it, and
            # stranded the `.part`. A single throttled range must never be able
            # to kill the whole transfer that way.
            retryable = [error for error in errors.exceptions if isinstance(error, TransportRetryError)]
            if retryable:
                # A concurrent throttle must not cost durable progress or fall
                # back to single-stream; surface the retryable signal instead.
                raise retryable[0]
            terminal = [error for error in errors.exceptions if isinstance(error, TransportSignalError)]
            if terminal:
                raise terminal[0]
            unsupported = errors.subgroup(RangeUnsupported)
            if unsupported is None:
                raise
            fallback_reason = str(unsupported.exceptions[0])
            range_failure = True

        if range_failure:
            self._record_range_fallback(item, size, part, fallback_reason or "unsupported range")
            part.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            if transform:
                target = destination.with_name(destination.name + ".encrypted")
                await self._download_single_with_retries(item, target, progress, control, loop, route_profile)
                await asyncio.to_thread(self._postprocess, target, destination, transform)
                return destination
            return await self._download_single_with_retries(item, destination, progress, control, loop, route_profile)

        if not coordinator.is_all_done():
            raise RuntimeError("segmented transfer ended before all bytes arrived")
        if progress:
            progress(total)
        self._verify(destination=part, item=item, expected_size=size)
        _replace_verified(part, destination)
        manifest_path.unlink(missing_ok=True)
        return destination

    def _download_single(self, item, destination, progress, control, loop, route_profile=None) -> Path:
        part = destination.with_name(destination.name + ".part")
        offset = part.stat().st_size if part.exists() else 0
        headers = self._request_headers(item)
        if offset:
            headers["Range"] = f"bytes={offset}-"
            validator = (item.metadata or {}).get("etag") or (item.metadata or {}).get("last_modified")
            if validator:
                headers["If-Range"] = validator
        request = urllib.request.Request(item.direct_url, headers=headers)
        try:
            response = self._open(request, 60, route_profile)
        except urllib.error.HTTPError as exc:
            self._record_response_headers(item, getattr(exc, "headers", None), getattr(item, "item_id", "download"))
            signal = self._http_error_signal(exc)
            exc.close()
            if offset and signal.status_code == 416:
                response = None
            else:
                raise self._transport_exception(signal, str(exc)) from exc
        if response is None:
            # A 416 means the server considers the persisted part complete;
            # verify it in place before atomically publishing it. Publishing
            # first would expose a corrupt part as the final destination if
            # checksum validation failed.
            self._verify(part, item, item.size)
            _replace_verified(part, destination)
            return destination
        try:
            self._record_response_headers(item, getattr(response, "headers", None), getattr(item, "item_id", "download"))
            status = getattr(response, "status", getattr(response, "code", 200))
            if offset and status == 416:
                self._verify(part, item, item.size)
                _replace_verified(part, destination)
                return destination
            if status not in {200, 206}:
                signal = self._response_signal(status, response.headers)
                raise self._transport_exception(signal)
            resumed = bool(offset and status == 206)
            if offset and not resumed:
                offset = 0
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type.startswith("text/html") and Path(destination).suffix.lower() not in {".html", ".htm"}:
                raise RuntimeError("server returned an HTML error page instead of a file")
            if resumed:
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(f"bytes {offset}-"):
                    raise RangeUnsupported("server returned an invalid resume Content-Range")
            content_length = response.headers.get("Content-Length")
            expected_wire: int | None = None
            if content_length and content_length.isdigit():
                wire_len = int(content_length)
                if item.size is None:
                    item.size = wire_len + (offset if resumed else 0)
                expected_wire = item.size - offset if resumed else item.size
                if wire_len != expected_wire:
                    raise IOError(f"response length mismatch: expected {expected_wire}, got {content_length}")
            elif item.size is not None:
                expected_wire = item.size - offset if resumed else item.size
            mode = "ab" if resumed else "wb"
            hasher = None
            if item.checksum and not resumed:
                alg = item.checksum.partition(":")[0] or "sha256"
                try:
                    hasher = hashlib.new(alg)
                except Exception:
                    hasher = None

            with part.open(mode) as handle:
                total = offset
                last_progress_time = time.monotonic()
                while True:
                    self._check_control(control)
                    if expected_wire is not None:
                        remaining = expected_wire - (total - offset)
                        if remaining <= 0:
                            # Reached expected byte count; exit without waiting for keepalive socket shutdown
                            break
                        to_read = min(1024 * 1024, remaining)
                    else:
                        to_read = 1024 * 1024

                    chunk = response.read(to_read)
                    if not chunk:
                        if expected_wire is not None and (total - offset) < expected_wire:
                            raise IOError(f"stream truncated: expected {expected_wire} bytes, received {total - offset}")
                        break

                    self._consume_bandwidth(loop, len(chunk))
                    handle.write(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    total += len(chunk)
                    if progress:
                        now_m = time.monotonic()
                        if now_m - last_progress_time >= 0.250:
                            progress(total)
                            last_progress_time = now_m
            if progress:
                progress(total)
            if item.size is None:
                item.size = total
            in_flight_digest = hasher.hexdigest() if hasher else None
            self._verify(part, item, item.size, in_flight_digest=in_flight_digest)
            _replace_verified(part, destination)
            return destination
        finally:
            response.close()

    def _fetch_range_stealer(
        self,
        item,
        path: Path,
        seg: SegmentState,
        start: int,
        end: int,
        control,
        loop,
        route_profile=None,
        coordinator: DynamicSegmentCoordinator | None = None,
        report_cb=None,
        transform=None,
    ) -> int:
        headers = self._request_headers(item)
        headers["Range"] = f"bytes={start}-{end}"
        validator = (item.metadata or {}).get("validator") or (item.metadata or {}).get("etag") \
            or (item.metadata or {}).get("last_modified")
        if validator:
            headers["If-Range"] = validator
        try:
            response = self._open(urllib.request.Request(item.direct_url, headers=headers), 60, route_profile)
        except urllib.error.HTTPError as exc:
            self._record_response_headers(item, getattr(exc, "headers", None), getattr(item, "item_id", "download"))
            signal = self._http_error_signal(exc)
            exc.close()
            raise self._transport_exception(signal, str(exc)) from exc
        try:
            self._record_response_headers(item, getattr(response, "headers", None), getattr(item, "item_id", "download"))
            status = getattr(response, "status", getattr(response, "code", 200))
            if status == 200:
                raise RangeUnsupported("server returned HTTP 200 instead of requested 206 Partial Content")
            if status != 206:
                # 429/403/5xx are classified before range validation so a
                # throttle can never masquerade as an unsupported range.
                signal = self._response_signal(status, response.headers)
                raise self._transport_exception(signal)
            content_range = response.headers.get("Content-Range", "")
            if not RangePlanner.validate_content_range(content_range, start, end, None):
                raise RangeUnsupported("server returned an invalid Content-Range")
            if "/" in content_range:
                total_text = content_range.rsplit("/", 1)[1]
                if total_text.isdigit() and int(total_text) < end + 1:
                    raise RangeUnsupported("range response total is smaller than its bounds")
            else:
                raise RangeUnsupported("server returned Content-Range without total size")
            response_validator = response.headers.get("ETag") or response.headers.get("Last-Modified")
            if validator and response_validator and response_validator != validator:
                raise RangeUnsupported("range response validator changed")
            response_length = response.headers.get("Content-Length")
            if response_length and response_length.isdigit() and int(response_length) != end - start + 1:
                raise RangeUnsupported("range response length does not match its bounds")

            written = 0
            # Open file handle once for positional writing chunks into allocated space without lock contention
            with path.open("r+b", buffering=0) as handle:
                while True:
                    self._check_control(control)
                    # Notice: Check current seg.end dynamically because work-stealing may truncate seg.end!
                    current_end = seg.end
                    current_expected = current_end - seg.start + 1
                    remaining = current_expected - seg.done
                    if remaining <= 0:
                        break

                    to_read = min(1024 * 1024, remaining)
                    chunk = response.read(to_read)
                    current_end = seg.end
                    current_expected = current_end - seg.start + 1
                    if not chunk:
                        if seg.done < current_expected:
                            raise IOError("range response was truncated")
                        break

                    # Guard if chunk extends past current dynamically shortened seg.end
                    remaining = current_expected - seg.done
                    if remaining <= 0:
                        break
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]

                    self._consume_bandwidth(loop, len(chunk))
                    write_offset = seg.start + seg.done
                    if transform:
                        chunk = self._decrypt_mega_ctr_chunk(chunk, write_offset, transform)
                    handle.seek(write_offset)
                    handle.write(chunk)
                    chunk_len = len(chunk)
                    written += chunk_len
                    
                    if coordinator:
                        coordinator.record_progress(seg.id, chunk_len)
                    else:
                        seg.done += chunk_len

                    if report_cb:
                        asyncio.run_coroutine_threadsafe(report_cb(chunk_len), loop).result()

                    if seg.done >= seg.expected:
                        break

            return written
        finally:
            response.close()

    def _fetch_range(self, item, path, start, end, control, loop, route_profile=None) -> int:
        headers = self._request_headers(item)
        headers["Range"] = f"bytes={start}-{end}"
        try:
            response = self._open(urllib.request.Request(item.direct_url, headers=headers), 60, route_profile)
        except urllib.error.HTTPError as exc:
            self._record_response_headers(item, getattr(exc, "headers", None), getattr(item, "item_id", "download"))
            signal = self._http_error_signal(exc)
            exc.close()
            raise self._transport_exception(signal, str(exc)) from exc
        try:
            self._record_response_headers(item, getattr(response, "headers", None), getattr(item, "item_id", "download"))
            status = getattr(response, "status", getattr(response, "code", 200))
            if status == 200:
                raise RangeUnsupported("server returned HTTP 200 instead of requested 206 Partial Content")
            if status != 206:
                signal = self._response_signal(status, response.headers)
                raise self._transport_exception(signal)
            expected = end - start + 1
            content_range = response.headers.get("Content-Range", "")
            if not content_range.startswith(f"bytes {start}-{end}/"):
                raise RangeUnsupported("server returned an invalid Content-Range")
            written = 0
            with path.open("r+b") as handle:
                handle.seek(start)
                while written < expected:
                    self._check_control(control)
                    chunk = response.read(min(1024 * 1024, expected - written))
                    if not chunk:
                        raise IOError("range response was truncated")
                    self._consume_bandwidth(loop, len(chunk))
                    handle.write(chunk)
                    written += len(chunk)
            if written != expected:
                raise IOError("range length mismatch")
            return written
        finally:
            response.close()

    def _probe(self, item, route_profile=None) -> dict[str, int | bool | str | None]:
        headers = self._request_headers(item)
        try:
            result = self.transport_pool.probe(item.direct_url, headers=headers, route_profile=route_profile)
            response_headers = result.get("headers", {}) or {}
            from .challenge_classifier import observe_received_response
            observe_received_response(
                source="custom_downloader.pool_probe", status=result.get("status_code"),
                headers=response_headers, final_url=item.direct_url or "",
            )
            host = (urlsplit(item.direct_url or "").hostname or "").lower()
            if host and response_headers:
                from .concurrency_auditor import concurrency_auditor
                concurrency_auditor.record_response_headers(host, getattr(item, "item_id", "probe"), dict(response_headers))
            probed_status = result.get("status_code")
            if probed_status is not None and int(probed_status) not in {200, 206, 416}:
                signal = classify_transport_signal(probed_status, "", self._header_map(response_headers))
                if signal.category in TRANSPORT_RETRY_CATEGORIES:
                    raise TransportRetryError(signal)
            probed_size = result.get("size")
            if probed_size is not None and getattr(item, "size", None) is None:
                item.size = int(probed_size)
            return {**result,
                    "etag": result.get("etag") or response_headers.get("etag") or response_headers.get("ETag"),
                    "last_modified": result.get("last_modified") or response_headers.get("last-modified") or response_headers.get("Last-Modified"),
                    "validator": result.get("validator"),
                    "content_type": response_headers.get("content-type") or response_headers.get("Content-Type")}
        except (TransportRetryError, TransportSignalError):
            raise
        except Exception:
            # Fallback to stdlib urllib if transport pool fails
            request = urllib.request.Request(item.direct_url, method="HEAD", headers=headers)
            try:
                response = self._open(request, 20, route_profile)
            except (urllib.error.HTTPError, urllib.error.URLError) as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    self._record_response_headers(item, getattr(exc, "headers", None), getattr(item, "item_id", "probe"))
                    signal = self._http_error_signal(exc)
                    if signal.category in TRANSPORT_RETRY_CATEGORIES:
                        exc.close()
                        raise TransportRetryError(signal, str(exc)) from exc
                request = urllib.request.Request(item.direct_url, headers={**headers, "Range": "bytes=0-0"})
                try:
                    response = self._open(request, 20, route_profile)
                except urllib.error.HTTPError as range_exc:
                    self._record_response_headers(item, getattr(range_exc, "headers", None), getattr(item, "item_id", "probe"))
                    signal = self._http_error_signal(range_exc)
                    if signal.category in TRANSPORT_RETRY_CATEGORIES:
                        range_exc.close()
                        raise TransportRetryError(signal, str(range_exc)) from range_exc
                    raise
            try:
                self._record_response_headers(item, getattr(response, "headers", None), getattr(item, "item_id", "probe"))
                from .challenge_classifier import observe_received_response
                observe_received_response(
                    source="custom_downloader.fallback_probe", status=getattr(response, "status", None),
                    headers=dict(getattr(response, "headers", {}) or {}), final_url=response.geturl() or item.direct_url or "",
                )
                length = response.headers.get("Content-Length")
                content_range = response.headers.get("Content-Range", "")
                size = int(length) if length and length.isdigit() else None
                if "/" in content_range:
                    suffix = content_range.rsplit("/", 1)[1]
                    if suffix.isdigit():
                        size = int(suffix)
                if size is not None and getattr(item, "size", None) is None:
                    item.size = int(size)
                content_range = response.headers.get("Content-Range", "")
                return {"size": size, "ranges": response.status == 206 and content_range.lower().startswith("bytes 0-0/"),
                        "status_code": response.status, "range_requested": "Range" in request.headers,
                        "content_range": content_range, "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                        "validator": response.headers.get("ETag") or response.headers.get("Last-Modified"),
                        "content_type": response.headers.get("Content-Type")}
            finally:
                response.close()

    @staticmethod
    def _split_ranges(size: int, count: int) -> list[tuple[int, int]]:
        count = max(1, min(count, size))
        base, extra = divmod(size, count)
        result = []
        start = 0
        for index in range(count):
            length = base + (1 if index < extra else 0)
            result.append((start, start + length - 1))
            start += length
        return result

    @staticmethod
    def _load_manifest(path: Path, size: int, ranges: list[tuple[int, int]]) -> list[dict[str, int]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("size") == size and value.get("ranges") == [{"start": a, "end": b} for a, b in ranges]:
                return value["segments"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return [{"start": start, "end": end, "done": 0} for start, end in ranges]

    @staticmethod
    def _save_manifest(path: Path, manifest: list[dict[str, int]]) -> None:
        payload = {"size": sum(e["end"] - e["start"] + 1 for e in manifest), "ranges": [{"start": e["start"], "end": e["end"]} for e in manifest], "segments": manifest}
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _verify(destination: Path, item: ResolvedItem, expected_size: int | None, in_flight_digest: str | None = None) -> None:
        if expected_size is not None and destination.stat().st_size != expected_size:
            raise RuntimeError(f"download size mismatch: expected {expected_size}, got {destination.stat().st_size}")
        if item.checksum:
            algorithm, _, expected = item.checksum.partition(":")
            if in_flight_digest:
                calc_digest = in_flight_digest
            else:
                digest = hashlib.new(algorithm or "sha256")
                with destination.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                calc_digest = digest.hexdigest()
            if calc_digest.lower() != expected.lower():
                raise RuntimeError("download checksum mismatch")

    @staticmethod
    def _check_control(control) -> None:
        if control and control.cancel.is_set():
            raise DownloadCanceled()
        if control and control.pause.is_set():
            raise DownloadPaused()

    async def _download_single_with_retries(self, item, destination, progress, control, loop, route_profile=None) -> Path:
        last_backoff = 0.0
        for attempt in range(self.resources.policy.max_retries + 1):
            circuit = None
            try:
                circuit = await self.resources.circuit_for(item.direct_url)
                await circuit.before()
                await self.resources.acquire_request(item.direct_url)
                result = await asyncio.to_thread(self._download_single, item, destination, progress, control, loop, route_profile)
                await circuit.record(True)
                return result
            except (DownloadPaused, DownloadCanceled):
                raise
            except TransportSignalError:
                if circuit:
                    await circuit.record(False)
                raise
            except RetryAfterError as exc:
                if circuit:
                    await circuit.record(False)
                if attempt >= self.resources.policy.max_retries:
                    raise
                last_backoff = self._backoff_delay(exc, last_backoff)
                await asyncio.sleep(last_backoff)
            except CircuitOpenError:
                if attempt >= self.resources.policy.max_retries:
                    raise
                last_backoff = decorrelated_jitter(last_backoff, base_seconds=1.0, max_seconds=15.0)
                await asyncio.sleep(last_backoff)
            except (OSError, ValueError, TypeError):
                if attempt >= self.resources.policy.max_retries:
                    raise
                last_backoff = decorrelated_jitter(last_backoff, base_seconds=1.0, max_seconds=30.0)
                await asyncio.sleep(last_backoff)
            except Exception:
                if circuit:
                    await circuit.record(False)
                if attempt >= self.resources.policy.max_retries:
                    raise
                last_backoff = decorrelated_jitter(last_backoff, base_seconds=1.0, max_seconds=30.0)
                await asyncio.sleep(last_backoff)
        raise AssertionError("unreachable")

    @staticmethod
    def _header_map(headers_source) -> dict[str, str]:
        headers = getattr(headers_source, "headers", headers_source)
        if headers is None:
            return {}
        if hasattr(headers, "items"):
            try:
                return {str(key): str(value) for key, value in headers.items()}
            except Exception:
                return {}
        return dict(headers) if isinstance(headers, dict) else {}

    @classmethod
    def _response_signal(cls, status, headers_source) -> TransportSignal:
        return classify_transport_signal(status, "", cls._header_map(headers_source))

    @classmethod
    def _http_error_signal(cls, exc) -> TransportSignal:
        status = getattr(exc, "code", None)
        headers = cls._header_map(getattr(exc, "headers", None))
        text = ""
        if status == 403:
            # A 403 block page is a throttle when its body is an HTML interstitial.
            try:
                body = exc.read(4096)
                if isinstance(body, (bytes, bytearray)):
                    text = bytes(body).decode("utf-8", "ignore")
            except Exception as read_error:
                logger.debug("403 response body probe failed: %s", read_error)
        return classify_transport_signal(status, text, headers)

    @staticmethod
    def _transport_exception(signal: TransportSignal, message: str | None = None) -> Exception:
        if signal.category in TRANSPORT_RETRY_CATEGORIES:
            return TransportRetryError(signal, message)
        return TransportSignalError(signal, message)

    @staticmethod
    def _backoff_delay(error: RetryAfterError, previous: float, *, base: float = 1.0,
                       cap: float = 60.0) -> float:
        if error.delay > 0:
            return min(error.delay, 300.0)
        return decorrelated_jitter(previous, base_seconds=base, max_seconds=cap)

    @staticmethod
    def _record_range_fallback(item, size: int | None, part: Path, reason: str) -> None:
        """Emit structured telemetry before a genuinely unsupported range is discarded."""
        try:
            from .telemetry import telemetry_bus
            telemetry_bus.record(
                level="WARN",
                subsystem="engine:custom_downloader",
                message="[RANGE_FALLBACK] discarding segmented partial after genuine range mismatch",
                context={
                    "item_id": getattr(item, "item_id", None),
                    "size": int(size) if size is not None else None,
                    "partial_bytes": part.stat().st_size if part.exists() else 0,
                    "reason": str(reason)[:300],
                },
                tier="engine",
            )
        except Exception as telemetry_error:
            logger.warning("range fallback telemetry failed: %s", telemetry_error)

    @staticmethod
    def _record_response_headers(item, headers_source, task_id: str | None = None) -> None:
        try:
            if not headers_source:
                return
            host = (urlsplit(getattr(item, "direct_url", "") or "").hostname or "").lower()
            if not host:
                return
            headers = getattr(headers_source, "headers", headers_source)
            if hasattr(headers, "items"):
                h_dict = dict(headers.items())
            elif isinstance(headers, dict):
                h_dict = headers
            else:
                return
            if h_dict:
                from .concurrency_auditor import concurrency_auditor
                t_id = task_id or getattr(item, "item_id", "task")
                concurrency_auditor.record_response_headers(host, t_id, h_dict)
        except Exception:
            pass

    @staticmethod
    def _request_headers(item) -> dict[str, str]:
        headers = dict(item.headers)
        if item.cookies and "Cookie" not in headers:
            headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in item.cookies.items())
        return headers

    def _check_disk(self, destination: Path, size: int) -> None:
        free = shutil.disk_usage(destination.parent).free
        if free < size + self.resources.policy.min_free_space:
            raise OSError(f"not enough free space for download: need {size + self.resources.policy.min_free_space} bytes")

    @staticmethod
    def _unique_destination(destination: Path) -> Path:
        stem, suffix = destination.stem, destination.suffix
        for index in range(1, 10_000):
            candidate = destination.with_name(f"{stem} ({index}){suffix}")
            if not candidate.exists() and not candidate.with_name(candidate.name + ".part").exists():
                return candidate
        raise FileExistsError("could not find a unique destination filename")

    def _consume_bandwidth(self, loop, amount: int) -> None:
        # An unlimited transfer must stay on the worker thread.  Crossing the
        # asyncio boundary for every 1 MiB chunk adds measurable overhead to
        # single-stream provider downloads (notably Transfer.it's encrypted
        # postprocess path) while the limiter is disabled.
        if self.resources.policy.bandwidth_bytes_per_second <= 0:
            return
        future = asyncio.run_coroutine_threadsafe(self.resources.bandwidth.acquire(amount), loop)
        future.result()

    @staticmethod
    def _open(request, timeout: float, route_profile: dict | None = None):
        """Open through the task's own route; Docker/SOCKS lifecycle is external."""
        endpoint = proxy_url_for(route_profile)
        if endpoint is None:
            from . import http_client
            headers = dict(request.header_items())
            method = getattr(request, "method", None) or ("HEAD" if request.get_method() == "HEAD" else "GET")
            # proxy=None, not the active route: this task's route is kernel-routed.
            return http_client.request(
                method,
                request.full_url,
                headers=headers,
                timeout=timeout,
                stream=True,
                proxy=None,
            )
        kind = route_profile.get("kind")
        if kind == "wireguard":
            kind = "socks5"  # a transfer-core tunnel, reached as a local SOCKS port
        if kind == "http_proxy":
            opener = route_http.opener(proxy=endpoint)
            return opener.open(request, timeout=timeout)
        if kind in {"socks5", "docker_socks5"}:
            try:
                import httpx
            except ImportError as exc:
                raise RuntimeError("SOCKS5 routes require the httpx[socks] dependency") from exc
            method = getattr(request, "method", None) or ("HEAD" if request.get_method() == "HEAD" else "GET")
            headers = dict(request.header_items())
            client = httpx.Client(proxy=endpoint, timeout=timeout, follow_redirects=True)
            response = client.send(client.build_request(method, request.full_url, headers=headers), stream=True)
            if response.status_code >= 400:
                status = response.status_code
                response.close(); client.close()
                raise urllib.error.HTTPError(request.full_url, status, f"server returned HTTP {status}", response.headers, None)
            return _HttpxResponse(client, response)
        raise ValueError(f"route kind {kind!r} is not supported by the HTTP backend")

    @staticmethod
    def _postprocess(source: Path, destination: Path, metadata: dict) -> None:
        if metadata.get("type") != "mega-ctr":
            raise RuntimeError(f"unsupported postprocess type: {metadata.get('type')}")
        try:
            cipher = CustomAsyncBackend._mega_ctr_cipher(metadata)
        except ImportError as exc:
            raise RuntimeError("PyCryptodome is required for encrypted MEGA-family downloads") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        with source.open("rb") as source_handle, part.open("wb") as destination_handle:
            while chunk := source_handle.read(1024 * 1024):
                destination_handle.write(cipher.decrypt(chunk))
        source.unlink(missing_ok=True)
        part.replace(destination)

    @staticmethod
    def _mega_ctr_cipher(metadata: dict, offset: int = 0):
        from Crypto.Cipher import AES
        from Crypto.Util import Counter

        key = [int(value) for value in metadata.get("key_a32", [])]
        if len(key) < 6:
            raise RuntimeError("encrypted download has an invalid file key")
        aes_key = b"".join((key[i] ^ key[i + 4] if len(key) >= 8 else key[i]).to_bytes(4, "big") for i in range(4))
        nonce = b"".join(value.to_bytes(4, "big") for value in key[4:6])
        block, intra = divmod(max(0, int(offset)), AES.block_size)
        cipher = AES.new(aes_key, AES.MODE_CTR, counter=Counter.new(64, prefix=nonce, initial_value=block))
        if intra:
            cipher.decrypt(b"\0" * intra)
        return cipher

    @staticmethod
    def _decrypt_mega_ctr_chunk(chunk: bytes, offset: int, metadata: dict) -> bytes:
        return CustomAsyncBackend._mega_ctr_cipher(metadata, offset).decrypt(chunk)
