from __future__ import annotations

import asyncio
import logging
import math
import random
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def normalize_bandwidth_rate(value: object, *, unit: str = "bytes_per_second") -> int:
    """Normalize every limiter input to an integer byte-per-second rate.

    ``0`` is the only unlimited value. UI-facing MiB/s values must opt into
    the explicit binary conversion instead of being passed as an ambiguous
    number of bytes or bits.
    """
    if unit not in {"bytes_per_second", "mebibytes_per_second"}:
        raise ValueError("bandwidth unit must be bytes_per_second or mebibytes_per_second")
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("bandwidth rate must be numeric")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("bandwidth rate must be numeric") from None
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("bandwidth rate must be finite and non-negative")
    if unit == "mebibytes_per_second":
        decimal *= 1024 * 1024
    if decimal != decimal.to_integral_value():
        raise ValueError("bandwidth rate must resolve to whole bytes per second")
    return int(decimal)


class AdaptivePermit:
    def __init__(self, controller: "AdaptiveTransferController", key: str) -> None:
        self.controller, self.key, self.started = controller, key, time.monotonic()
        self._released = False

    def finish(self, success: bool = True, *, status_code: int | None = None,
               retry_after: float | None = None) -> None:
        if not self._released:
            self._released = True
            self.controller.finish(self, success, status_code=status_code, retry_after=retry_after,
                                   elapsed=time.monotonic() - self.started)

    def __enter__(self) -> "AdaptivePermit":
        return self

    def __exit__(self, exc_type, _value, _traceback) -> None:
        self.finish(exc_type is None, status_code=getattr(_value, "status_code", None),
                    retry_after=getattr(_value, "retry_after", None))


class AdaptiveTransferController:
    """Shared AIMD admission and launch pacing for plugin calls and transfers."""

    def __init__(self, global_concurrency: int = 8, default_concurrency: int = 2) -> None:
        self.global_concurrency = max(1, int(global_concurrency))
        self.default_concurrency = max(1, int(default_concurrency))
        self._condition = threading.Condition()
        self._global_in_flight = 0
        self._states: dict[str, dict[str, float | int]] = {}

    def configure(self, key: str, *, ceiling: int | None = None,
                  requests_per_second: float | None = None,
                  initial_concurrency: int | None = None) -> None:
        with self._condition:
            is_new = key not in self._states
            state = self._states.setdefault(key, {
                "concurrency": self.default_concurrency,
                "ceiling": self.default_concurrency,
                "rps": 0.0,
                "in_flight": 0,
                "successes": 0,
                "failures": 0,
                "cooldown_until": 0.0,
                "next_start": 0.0,
                "ewma_latency": 0.0,
            })
            if ceiling is not None:
                state["ceiling"] = max(1, int(ceiling))
                state["concurrency"] = min(int(state["concurrency"]), int(state["ceiling"]))
            if is_new and initial_concurrency is not None:
                state["concurrency"] = min(
                    int(state["ceiling"]), max(1, int(initial_concurrency)))
            if requests_per_second is not None:
                state["rps"] = max(0.0, float(requests_per_second))

    def acquire(self, key: str, *, ceiling: int | None = None,
                requests_per_second: float | None = None,
                initial_concurrency: int | None = None) -> AdaptivePermit:
        self.configure(
            key,
            ceiling=ceiling,
            requests_per_second=requests_per_second,
            initial_concurrency=initial_concurrency,
        )
        with self._condition:
            state = self._states[key]
            while True:
                now = time.monotonic()
                slot_ready = int(state["in_flight"]) < int(state["concurrency"]) and self._global_in_flight < self.global_concurrency
                start_ready = max(float(state["cooldown_until"]), float(state["next_start"])) <= now
                if slot_ready and start_ready:
                    state["in_flight"] = int(state["in_flight"]) + 1
                    self._global_in_flight += 1
                    rps = float(state["rps"])
                    state["next_start"] = now + (1.0 / rps if rps > 0 else 0.0)
                    return AdaptivePermit(self, key)
                waits = [0.05]
                if not slot_ready:
                    waits.append(0.1)
                waits.append(max(0.0, float(state["cooldown_until"]) - now))
                waits.append(max(0.0, float(state["next_start"]) - now))
                self._condition.wait(timeout=min(max(waits), 2.0))

    def finish(self, permit: AdaptivePermit, success: bool, *, status_code: int | None = None,
               retry_after: float | None = None, elapsed: float = 0.0) -> None:
        with self._condition:
            state = self._states.get(permit.key)
            if not state:
                return
            state["in_flight"] = max(0, int(state["in_flight"]) - 1)
            self._global_in_flight = max(0, self._global_in_flight - 1)
            if elapsed > 0:
                previous = float(state["ewma_latency"])
                state["ewma_latency"] = elapsed if not previous else previous * 0.8 + elapsed * 0.2
            if success:
                state["successes"] = int(state["successes"]) + 1
                state["failures"] = 0
                if int(state["successes"]) >= 5:
                    state["concurrency"] = min(int(state["ceiling"]), int(state["concurrency"]) + 1)
                    state["successes"] = 0
            else:
                state["failures"] = int(state["failures"]) + 1
                state["successes"] = 0
                state["concurrency"] = max(1, math.ceil(int(state["concurrency"]) / 2))
                retry = float(retry_after or 0)
                if retry <= 0:
                    retry = min(60.0, 2.0 ** min(int(state["failures"]) - 1, 5))
                state["cooldown_until"] = max(float(state["cooldown_until"]), time.monotonic() + retry + random.uniform(0.05, 0.25))
            self._condition.notify_all()

    def snapshot(self) -> list[dict[str, object]]:
        with self._condition:
            return [{"key": key, "concurrency": int(value["concurrency"]), "ceiling": int(value["ceiling"]),
                     "in_flight": int(value["in_flight"]), "failures": int(value["failures"]),
                     "cooldown_remaining": max(0.0, float(value["cooldown_until"]) - time.monotonic()),
                     "ewma_latency": round(float(value["ewma_latency"]), 4)} for key, value in sorted(self._states.items())]


class CircuitOpenError(RuntimeError):
    pass


class AdaptiveCircuitBreaker:
    """Per-host fail-fast circuit to avoid hammering a provider during outages."""

    def __init__(self, threshold: int = 3, cooldown: float = 30.0) -> None:
        self.threshold = max(1, threshold)
        self.cooldown = max(1.0, cooldown)
        self.failures = 0
        self.opened_until = 0.0
        self._lock = asyncio.Lock()

    async def before(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self.opened_until > now:
                raise CircuitOpenError("host circuit is open; retry is temporarily suppressed")
            if self.opened_until:
                self.opened_until = 0.0
                self.failures = 0

    async def record(self, success: bool) -> None:
        async with self._lock:
            if success:
                self.failures = 0
                self.opened_until = 0.0
            else:
                self.failures += 1
                if self.failures >= self.threshold:
                    self.opened_until = time.monotonic() + self.cooldown

    async def reset(self) -> None:
        async with self._lock:
            self.failures = 0
            self.opened_until = 0.0


@dataclass(slots=True)
class SchedulerPolicy:
    """Hard safety limits shared by every transfer backend."""

    max_active_tasks: int = 4
    max_active_segments: int = 16
    # The default UI setting permits eight connections per file. Keep the
    # host-wide safety cap aligned with that setting so segmented transfers do
    # not silently serialize behind a two-connection legacy default.
    per_host_transfers: int = 8
    per_host_requests_per_second: int = 8
    per_provider_transfers: int = 2
    per_account_transfers: int = 1
    # Segmented transfers may start conservatively and let the host window
    # grow. Backends can opt into a capability-appropriate initial width while
    # the existing bounded defaults remain unchanged.
    initial_segment_concurrency: int = 2
    provider_limits: dict[str, int] = field(default_factory=lambda: {
        "google-drive": 2,
        "1fichier": 1,
        "rapidgator": 1,
        "ddownload": 1,
        "krakenfiles": 2,
        "pixeldrain": 2,
        "mega": 2,
        # DataNodes serves several parts of a package at once; a reference client
        # that reliably runs four concurrently caps nothing per provider. The 1
        # here silently serialized every multipart package regardless of what the
        # host ceiling had learned.
        "datanodes": 4,
        "fuckingfast": 1,
        "filekeeper": 1,
        "mediafire": 2,
        "terabox": 1,
    })
    max_segments_per_file: int = 8
    min_segment_size: int = 16 * 1024 * 1024
    max_file_size: int = 50 * 1024**3
    max_package_size: int = 200 * 1024**3
    min_free_space: int = 256 * 1024 * 1024
    bandwidth_bytes_per_second: int = 0
    max_retries: int = 4
    request_timeout_seconds: int = 30
    daily_byte_quota: int = 0
    archive_max_output_bytes: int = 500 * 1024**3
    archive_free_fraction: float = 0.8
    archive_max_file_count: int = 100_000
    archive_max_nesting_depth: int = 32
    archive_max_compression_ratio: float = 1000.0

    def to_dict(self) -> dict[str, object]:
        return {
            name: getattr(self, name) for name in (
                "max_active_tasks", "max_active_segments", "per_host_transfers",
                "per_host_requests_per_second", "per_provider_transfers", "per_account_transfers",
                "initial_segment_concurrency",
                "provider_limits", "max_segments_per_file", "min_segment_size", "max_file_size",
                "max_package_size", "min_free_space", "bandwidth_bytes_per_second", "max_retries",
                "request_timeout_seconds",
                "daily_byte_quota", "archive_max_output_bytes", "archive_free_fraction",
                "archive_max_file_count", "archive_max_nesting_depth", "archive_max_compression_ratio"
            )
        }


class AsyncTokenBucket:
    def __init__(self, rate: int = 0, capacity: int | None = None) -> None:
        self.rate = normalize_bandwidth_rate(rate)
        self.capacity = capacity or max(self.rate, 1024 * 1024)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, amount: int) -> None:
        if self.rate <= 0 or amount <= 0:
            return
        remaining = int(amount)
        while remaining > 0:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                take = min(remaining, self.capacity)
                if self.tokens >= take:
                    self.tokens -= take
                    remaining -= int(take)
                    continue
                wait = (take - self.tokens) / self.rate
            await asyncio.sleep(min(max(wait, 0.001), 1.0))

    async def set_rate(self, rate: int) -> None:
        async with self._lock:
            self.rate = normalize_bandwidth_rate(rate)
            self.capacity = max(self.rate, 1024 * 1024)
            self.tokens = min(self.tokens, float(self.capacity))
            self.updated = time.monotonic()


class AdaptiveHostWindow:
    """A bounded AIMD window for one host, gated by the host pressure breaker."""

    def __init__(
        self,
        ceiling: int,
        initial_window: int = 1,
        host: str = "",
        auditor: object = None,
    ) -> None:
        self.ceiling = max(1, ceiling)
        self.window = min(self.ceiling, max(1, int(initial_window)))
        self.in_flight = 0
        self.successes = 0
        self.host = (host or "").lower()
        self.circuit: AdaptiveCircuitBreaker | None = None
        self._auditor = auditor
        self._condition = asyncio.Condition()

    def _auditor_ref(self):
        if self._auditor is not None:
            return self._auditor
        from .concurrency_auditor import concurrency_auditor
        return concurrency_auditor

    def frozen_limit(self) -> int | None:
        """Breaker-enforced limit for this host, or None when the breaker is closed."""
        if not self.host:
            return None
        try:
            return self._auditor_ref().breaker_admission_limit(self.host)
        except Exception as err:
            logger.warning("breaker admission lookup failed for %s: %s", self.host, err)
            return None

    def effective_window(self) -> int:
        limit = self.frozen_limit()
        if limit is None:
            return self.window
        return max(1, min(self.window, limit))

    async def acquire(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self.in_flight < self.effective_window())
            self.in_flight += 1

    async def warm_start(self, initial_window: int) -> None:
        """Open a prepared segmented transfer at its configured width.

        A frozen breaker caps the window at its baseline; warming a host must
        never reopen past the freeze.
        """
        async with self._condition:
            requested = min(self.ceiling, max(1, int(initial_window)))
            limit = self.frozen_limit()
            if limit is None:
                self.window = max(self.window, requested)
            else:
                self.window = max(1, min(requested, limit, self.effective_window()))
            self._condition.notify_all()

    async def release(
        self,
        success: bool,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        throttle: bool = False,
    ) -> None:
        async with self._condition:
            self.in_flight = max(0, self.in_flight - 1)
            if success:
                self.successes += 1
                if self.successes >= max(2, self.window * 2):
                    self.window = min(self.ceiling, self.window + 1)
                    self.successes = 0
            else:
                self.window = max(1, (self.window + 1) // 2)
                self.successes = 0
            self._condition.notify_all()
        if not success and (throttle or status_code == 429 or bool(retry_after and retry_after > 0)):
            circuit = self.circuit
            if circuit is not None:
                await circuit.record(False)

    def snapshot(self) -> dict[str, object]:
        """Window/breaker state for telemetry consumers."""
        frozen = self.frozen_limit()
        return {
            "host": self.host,
            "ceiling": self.ceiling,
            "window": self.window,
            "effective_window": self.effective_window(),
            "frozen_limit": frozen,
            "in_flight": self.in_flight,
            "successes": self.successes,
        }


def item_admission_limit(static_limit: int, verified_ceiling: int | None,
                         breaker_limit: int | None) -> int:
    """Combine optimistic capacity with authoritative pressure evidence.

    A historical success ceiling may demonstrate more capacity than a static
    fallback.  It must not narrow the fallback: old runs may have observed only
    one stream because an upstream gate admitted only one.  Explicit breaker
    evidence (429/403/cooldown) is the sole narrowing input.
    """
    limit = max(1, int(static_limit))
    if verified_ceiling is not None:
        limit = max(limit, max(1, int(verified_ceiling)))
    if breaker_limit is not None:
        limit = min(limit, max(1, int(breaker_limit)))
    return limit


@dataclass(slots=True)
class ResourceManager:
    policy: SchedulerPolicy = field(default_factory=SchedulerPolicy)
    active_tasks: asyncio.Semaphore = field(init=False)
    active_segments: asyncio.Semaphore = field(init=False)
    bandwidth: AsyncTokenBucket = field(init=False)
    hosts: dict[str, AdaptiveHostWindow] = field(default_factory=dict)
    request_buckets: dict[str, AsyncTokenBucket] = field(default_factory=dict)
    circuits: dict[str, AdaptiveCircuitBreaker] = field(default_factory=dict)
    provider_slots: dict[str, asyncio.Semaphore] = field(default_factory=dict)
    # Width each provider slot was built at. Without this the slot is created
    # once, at whatever limit happened to apply to the FIRST item, and can never
    # widen again -- so a provider whose host ceiling later rises stays pinned to
    # its opening width for the life of the process.
    provider_slot_widths: dict[str, int] = field(default_factory=dict)
    account_slots: dict[str, asyncio.Semaphore] = field(default_factory=dict)
    # Provider manifest request rates pace resolver/API calls. Payloads usually
    # run against a separate CDN host and must not inherit that page-request
    # delay. Keep the public attribute for compatibility with diagnostics.
    provider_rates: dict[str, float] = field(default_factory=dict)
    bandwidth_scope: str = "global"
    _host_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    _provider_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    adaptive: AdaptiveTransferController = field(init=False)

    def __post_init__(self) -> None:
        self.active_tasks = asyncio.Semaphore(self.policy.max_active_tasks)
        self.active_segments = asyncio.Semaphore(self.policy.max_active_segments)
        self.bandwidth = AsyncTokenBucket(self.policy.bandwidth_bytes_per_second)
        self.adaptive = AdaptiveTransferController(global_concurrency=self.policy.max_active_segments,
                                                   default_concurrency=max(1, self.policy.per_provider_transfers))

    async def set_bandwidth_rate(self, rate: int) -> None:
        """Change the shared limiter without interrupting active transfers."""
        self.policy.bandwidth_bytes_per_second = normalize_bandwidth_rate(rate)
        self.bandwidth_scope = "global"
        await self.bandwidth.set_rate(self.policy.bandwidth_bytes_per_second)

    def bandwidth_diagnostics(self, scope: str | None = None) -> dict[str, object]:
        return {
            "rate_bytes_per_second": int(self.policy.bandwidth_bytes_per_second),
            "scope": scope or self.bandwidth_scope,
            "unlimited": self.policy.bandwidth_bytes_per_second == 0,
            "unit": "bytes_per_second",
        }

    def configure_provider(self, provider: str, *, concurrency: int | None = None,
                           requests_per_second: float | None = None) -> None:
        """Apply provider manifest concurrency and resolver request hints."""
        provider_id = str(provider).lower()
        if concurrency is not None:
            self.policy.provider_limits[provider_id] = max(1, int(concurrency))
        if requests_per_second is not None:
            self.provider_rates[provider_id] = max(0.0, float(requests_per_second))
        self.adaptive.configure(
            f"{provider_id}:global:unknown",
            ceiling=self.policy.provider_limits.get(provider_id, self.policy.per_provider_transfers),
            requests_per_second=self.provider_rates.get(provider_id, 0.0),
        )

    async def host_window(self, url: str) -> AdaptiveHostWindow:
        host = (urlsplit(url).hostname or "unknown").lower()
        async with self._host_lock:
            window = self.hosts.get(host)
            if window is None:
                window = AdaptiveHostWindow(self.policy.per_host_transfers, host=host)
                self.hosts[host] = window
            window.host = host
            window.circuit = self.circuits.setdefault(host, AdaptiveCircuitBreaker())
            return window

    async def prepare_segment_window(self, url: str, width: int) -> AdaptiveHostWindow:
        """Warm the shared host cap once before launching segmented workers."""
        host = (urlsplit(url).hostname or "unknown").lower()
        async with self._host_lock:
            window = self.hosts.get(host)
            if window is None:
                window = AdaptiveHostWindow(self.policy.per_host_transfers, host=host)
                self.hosts[host] = window
            window.host = host
            window.circuit = self.circuits.setdefault(host, AdaptiveCircuitBreaker())
            await window.warm_start(width)
            return window

    def host_snapshots(self) -> list[dict[str, object]]:
        """Aggregate window/breaker snapshots for diagnostics and telemetry."""
        return [window.snapshot() for window in self.hosts.values()]

    async def acquire_request(self, url: str) -> None:
        host = (urlsplit(url).hostname or "unknown").lower()
        async with self._host_lock:
            bucket = self.request_buckets.setdefault(
                host,
                AsyncTokenBucket(self.policy.per_host_requests_per_second, self.policy.per_host_requests_per_second),
            )
        await bucket.acquire(1)

    async def circuit_for(self, url: str) -> AdaptiveCircuitBreaker:
        host = (urlsplit(url).hostname or "unknown").lower()
        async with self._host_lock:
            return self.circuits.setdefault(host, AdaptiveCircuitBreaker())

    async def reset_circuits(self) -> None:
        async with self._host_lock:
            for circuit in self.circuits.values():
                await circuit.reset()

    @asynccontextmanager
    async def item_slot(self, item):
        """Reserve provider/account capacity for the whole resolved item."""
        from .concurrency_auditor import concurrency_auditor
        provider = str(getattr(item, "provider", "unknown") or "unknown").lower()
        target_host = (urlsplit(getattr(item, "direct_url", "") or getattr(item, "source_url", "")).hostname or "").lower()
        
        static_limit = self.policy.provider_limits.get(provider, self.policy.per_provider_transfers)
        verified_ceiling = (
            concurrency_auditor.get_profile(target_host).verified_ceiling
            if target_host else None
        )
        breaker_limit = concurrency_auditor.breaker_admission_limit(target_host) if target_host else None
        limit = item_admission_limit(static_limit, verified_ceiling, breaker_limit)
        async with self._provider_lock:
            width = max(1, limit)
            provider_slot = self.provider_slots.get(provider)
            if provider_slot is None:
                provider_slot = asyncio.Semaphore(width)
                self.provider_slots[provider] = provider_slot
                self.provider_slot_widths[provider] = width
            else:
                # Grow only. The first item of a package often arrives before its
                # storage host is known, so it opens the slot at the provider's
                # static fallback; every later item then has to be able to widen
                # it once the host's real ceiling is established.
                opened_at = self.provider_slot_widths.get(provider, width)
                if width > opened_at:
                    for _ in range(width - opened_at):
                        provider_slot.release()
                    self.provider_slot_widths[provider] = width
            account_ref = (getattr(item, "metadata", {}) or {}).get("account_ref")
            account_key = f"{provider}:{account_ref}" if account_ref else None
            account_slot = None
            if account_key:
                account_slot = self.account_slots.setdefault(
                    account_key,
                    asyncio.Semaphore(max(1, self.policy.per_account_transfers)),
                )
        key = f"{provider}:{account_ref or '-'}:{target_host or 'unknown'}"
        permit = await asyncio.to_thread(
            self.adaptive.acquire,
            key,
            ceiling=limit,
            # Resolver/API pacing belongs to PluginRegistry.acquire(). Reusing
            # it here spaced DataNodes CDN payload starts two seconds apart even
            # after all four files had already resolved.
            requests_per_second=0.0,
            # A provider/storage limit is already an admission decision. Starting
            # another controller below it silently serializes large packages for
            # several complete files before AIMD can learn the same width.
            initial_concurrency=limit,
        )
        provider_acquired = False
        account_acquired = False
        try:
            try:
                await provider_slot.acquire()
                provider_acquired = True
                if account_slot is not None:
                    await account_slot.acquire()
                    account_acquired = True
            except BaseException as exc:
                permit.finish(False, status_code=getattr(exc, "status_code", None),
                              retry_after=getattr(exc, "retry_after", None))
                raise
            try:
                yield
                permit.finish(True)
            except BaseException as exc:
                permit.finish(False, status_code=getattr(exc, "status_code", None),
                              retry_after=getattr(exc, "retry_after", None))
                raise
            finally:
                if account_acquired and account_slot is not None:
                    account_slot.release()
        finally:
            if provider_acquired:
                provider_slot.release()
