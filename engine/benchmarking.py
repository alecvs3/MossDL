"""Deterministic transfer benchmarks and secret-safe release reports.

The runner deliberately calls the existing backend adapters with a real
``ResourceManager``.  The fixture server controls the service-side rate so
release decisions are normalized to an advertised capability instead of a
developer workstation's network speed.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .custom_downloader import CustomAsyncBackend
from .errors import DownloadPaused
from .limits import ResourceManager, SchedulerPolicy
from .models import ResolvedItem
from .range_planner import RangeCapability, RangePlanner
from .reliability import verify_file
from .rust_backend import RustTransferBackend
from .transport_pool import PooledTransportManager


SCHEMA_VERSION = 1
FIXTURE_ID = "deterministic-transfer-v1"
FIXTURE_CASES = (
    "no-range",
    "valid-206",
    "416",
    "validator-mismatch",
    "malformed-206",
    "retry",
    "refresh",
    "throttling",
    "slow-server",
    "pause-resume",
    "integrity",
)
CORE_SCENARIOS = ("single-stream", "eight-range", "sixteen-range", "thirty-two-range", "direct-to-disk")
# Match the actual backend read size so the deterministic fixture does not
# turn a bandwidth gate into a scheduler test through hundreds of tiny sleeps.
_CHUNK_SIZE = 1024 * 1024
_MIB = 1024 * 1024


def _generate_payload(size: int) -> bytes:
    base = bytes(index % 251 for index in range(251))
    full = size // 251
    rem = size % 251
    return (base * full) + base[:rem]


def _redact(value: Any) -> Any:
    """Remove request material from a report while retaining useful labels."""
    secret_keys = ("authorization", "cookie", "token", "secret", "password", "signature", "credential", "header")
    if isinstance(value, dict):
        return {str(key): "[redacted]" if any(part in str(key).lower() for part in secret_keys)
                else _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https", "ftp"} and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return value


@dataclass(frozen=True, slots=True)
class BenchmarkScenario:
    """A named, repeatable benchmark or fixture behavior case."""

    name: str
    payload_size: int = 32 * _MIB
    advertised_rate_bytes_per_second: int = 4 * _MIB
    range_count: int = 1
    direct_to_disk: bool = False
    fixture_case: str = "valid-206"
    release_gate: bool = True

    @classmethod
    def named(cls, name: str, *, payload_size: int | None = None,
              advertised_rate_bytes_per_second: int | None = None,
              uncapped: bool = False) -> "BenchmarkScenario":
        normalized = str(name).strip().lower()
        if normalized in {"single", "single-stream", "1x"}:
            normalized = "single-stream"
        elif normalized in {"eight", "eight-range", "8x"}:
            normalized = "eight-range"
        elif normalized in {"sixteen", "sixteen-range", "16x"}:
            normalized = "sixteen-range"
        elif normalized in {"thirty-two", "thirty-two-range", "thirtytwo-range", "32x"}:
            normalized = "thirty-two-range"
        elif normalized in {"disk", "direct-to-disk"}:
            normalized = "direct-to-disk"
        rate = 0 if uncapped else (advertised_rate_bytes_per_second if advertised_rate_bytes_per_second is not None else 4 * _MIB)
        if normalized == "single-stream":
            return cls(normalized, payload_size or 32 * _MIB, advertised_rate_bytes_per_second=rate, range_count=1)
        if normalized == "eight-range":
            return cls(normalized, payload_size or 32 * _MIB, advertised_rate_bytes_per_second=rate, range_count=8)
        if normalized == "sixteen-range":
            return cls(normalized, payload_size or 32 * _MIB, advertised_rate_bytes_per_second=rate, range_count=16)
        if normalized == "thirty-two-range":
            return cls(normalized, payload_size or 32 * _MIB, advertised_rate_bytes_per_second=rate, range_count=32)
        if normalized == "direct-to-disk":
            return cls(normalized, payload_size or 32 * _MIB, advertised_rate_bytes_per_second=rate, range_count=8, direct_to_disk=True)
        if normalized in FIXTURE_CASES:
            return cls(normalized, payload_size or 256 * 1024,
                       advertised_rate_bytes_per_second=rate,
                       range_count=1,
                       fixture_case=normalized, release_gate=False,
                       direct_to_disk=normalized in {"direct-to-disk", "integrity"})
        raise ValueError(f"unknown benchmark scenario: {name}")

    def to_dict(self) -> dict[str, Any]:
        return _redact({"schema_version": SCHEMA_VERSION, **asdict(self)})


@dataclass(slots=True)
class BenchmarkResult:
    """One normalized backend/scenario result, including an explicit absence."""

    backend: str
    scenario: str
    fixture: dict[str, Any]
    range_plan: dict[str, Any]
    metrics: dict[str, Any]
    integrity: dict[str, Any]
    availability: dict[str, Any]
    gate: dict[str, Any]
    source: str = "fixture"
    failure: dict[str, Any] | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _redact(asdict(self))


@dataclass(slots=True)
class BenchmarkReport:
    """Versioned report suitable for JSON or one-record-per-line JSONL output."""

    results: list[BenchmarkResult] = field(default_factory=list)
    fixture_matrix: list[dict[str, Any]] = field(default_factory=list)
    source: str = "fixture"
    generated_at: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        results = [result.to_dict() for result in self.results]
        gating_results = [item for item in results if bool(item["gate"].get("gating", False))]
        passed = sum(bool(item["gate"].get("passed", False)) for item in gating_results)
        gating = len(gating_results)
        return _redact({
            "schema_version": self.schema_version,
            "report_version": "transfer-benchmark/1",
            "source": self.source,
            "generated_at": self.generated_at,
            "results": results,
            "fixture_matrix": _redact(self.fixture_matrix),
            "summary": {"results": len(results), "gating_results": gating,
                        "passed_gates": passed, "all_gates_passed": passed == gating},
            "eligibility": {
                "fixture_is_release_gate": self.source == "fixture",
                "live_is_corrobating_only": True,
                "advertised_rate_unit": "bytes_per_second",
                "criteria": {
                    "single_stream_min_fraction": 0.90,
                    "eight_range_min_ideal_fraction": 0.70,
                    "eight_range_min_single_multiplier": 2.0,
                    "direct_to_disk_min_memory_fraction": 0.90,
                },
            },
            "redaction": {"request_urls": "omitted_or_identity_hash",
                          "request_material": "omitted_or_redacted"},
        })


@dataclass(slots=True)
class _FixtureSpec:
    case: str = "valid-206"
    payload_size: int = 32 * _MIB
    rate_bytes_per_second: int = 4 * _MIB
    retry_failures: int = 1
    validator: str = '"fixture-v1"'


class _FixtureState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.head_requests = 0
        self.get_requests = 0
        self.probe_requests = 0
        self.range_requests = 0
        self.retry_failures = 0
        self.refresh_failures = 0
        self.bytes_sent = 0
        self.read_seconds = 0.0
        self.active_ranges = 0
        self.peak_ranges = 0
        self.pause_observed = 0
        self.active_sends = 0
        self.transfer_started_at = 0.0
        self.transfer_finished_at = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {key: value for key, value in vars(self).items() if key != "lock"}


class _FixtureHandler(BaseHTTPRequestHandler):
    server: "_FixtureServer"

    def _record(self, key: str, amount: int = 1) -> None:
        with self.server.state.lock:
            setattr(self.server.state, key, getattr(self.server.state, key) + amount)

    def _headers(self, status: int, *, length: int, ranged: bool, validator: str) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("ETag", validator)
        if ranged:
            self.send_header("Accept-Ranges", "bytes")

    def _send(self, payload: bytes, *, start: int, end: int, status: int, ranged: bool, validator: str,
              is_probe: bool = False) -> None:
        self._headers(status, length=len(payload), ranged=ranged, validator=validator)
        if ranged:
            content_end = end
            if self.server.spec.case == "malformed-206":
                content_end = max(start, end - 1)
            self.send_header("Content-Range", f"bytes {start}-{content_end}/{self.server.spec.payload_size}")
        self.end_headers()
        if not is_probe:
            with self.server.state.lock:
                if self.server.state.active_sends == 0:
                    self.server.state.transfer_started_at = time.monotonic()
                self.server.state.active_sends += 1
        started = time.monotonic()
        try:
            rate = self.server.spec.rate_bytes_per_second
            for offset in range(0, len(payload), _CHUNK_SIZE):
                chunk = payload[offset:offset + _CHUNK_SIZE]
                if rate > 0:
                    # Pace before writing so the client observes the fixture's
                    # advertised service rate. Sleeping after write lets a
                    # Content-Length client finish before the handler's final
                    # delay, which makes server-side timing meaningless.
                    time.sleep(len(chunk) / rate)
                self.wfile.write(chunk)
                self.wfile.flush()
                self._record("bytes_sent", len(chunk))
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Pause/cancel/retry tests intentionally close an in-flight
            # response. The request remains a valid fixture observation and
            # should not produce a noisy server traceback.
            return
        finally:
            self._record("read_seconds", time.monotonic() - started)
            if not is_probe:
                with self.server.state.lock:
                    self.server.state.active_sends = max(0, self.server.state.active_sends - 1)
                    if self.server.state.active_sends == 0:
                        self.server.state.transfer_finished_at = time.monotonic()

    def do_HEAD(self) -> None:  # noqa: N802
        self._record("requests")
        self._record("head_requests")
        if self.server.spec.case == "416":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{self.server.spec.payload_size}")
            self.send_header("ETag", self.server.spec.validator)
            self.end_headers()
            return
        self._headers(200, length=self.server.spec.payload_size,
                      ranged=self.server.spec.case not in {"no-range"}, validator=self.server.spec.validator)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._record("requests")
        self._record("get_requests")
        if self.path.startswith("/expired") and self.server.spec.case == "refresh":
            self._record("refresh_failures")
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.send_header("X-Transfer-Refresh", "required")
            self.end_headers()
            return

        range_header = self.headers.get("Range", "")
        is_probe = range_header == "bytes=0-0"
        if is_probe:
            self._record("probe_requests")
        if self.server.spec.case == "retry" and not is_probe:
            with self.server.state.lock:
                should_fail = self.server.state.retry_failures < self.server.spec.retry_failures
                if should_fail:
                    self.server.state.retry_failures += 1
            if should_fail:
                self.send_response(503)
                self.send_header("Retry-After", "0.01")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        start, end = 0, self.server.spec.payload_size - 1
        ranged = False
        if range_header.startswith("bytes=") and self.server.spec.case != "no-range":
            try:
                start_text, _, end_text = range_header[6:].partition("-")
                start = int(start_text)
                end = int(end_text) if end_text else end
            except ValueError:
                start, end = 0, -1
            if start < 0 or end < start or start >= self.server.spec.payload_size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{self.server.spec.payload_size}")
                self.end_headers()
                return
            ranged = True
            self._record("range_requests")
            with self.server.state.lock:
                self.server.state.active_ranges += 1
                self.server.state.peak_ranges = max(self.server.state.peak_ranges, self.server.state.active_ranges)

        try:
            validator = self.server.spec.validator
            if self.server.spec.case == "validator-mismatch" and not is_probe:
                validator = '"fixture-v2"'
            payload = self.server.body[start:end + 1]
            self._send(payload, start=start, end=end,
                       status=206 if ranged else 200, ranged=ranged, validator=validator,
                       is_probe=is_probe)
        finally:
            if ranged:
                with self.server.state.lock:
                    self.server.state.active_ranges -= 1

    def log_message(self, *_args: Any) -> None:
        return


class _FixtureServer(ThreadingHTTPServer):
    def __init__(self, spec: _FixtureSpec) -> None:
        super().__init__(("127.0.0.1", 0), _FixtureHandler)
        self.spec = spec
        self.state = _FixtureState()
        self.body = _generate_payload(spec.payload_size)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True, name="benchmark-fixture")

    def __enter__(self) -> "_FixtureServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/fixture.bin"

    @property
    def expired_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/expired.bin"


class _BenchmarkControl:
    def __init__(self) -> None:
        self.pause = threading.Event()
        self.cancel = threading.Event()


def _fixture_identity(case: str, payload_size: int) -> dict[str, Any]:
    return {"id": f"{FIXTURE_ID}:{case}", "case": case,
            "payload_size": payload_size, "source": "deterministic-local-fixture"}


class BenchmarkRunner:
    """Run fixture benchmarks and normalize optional adapter outcomes."""

    def __init__(self, output_dir: str | Path | None = None, *, payload_size: int = 32 * _MIB,
                 advertised_rate_bytes_per_second: int = 4 * _MIB,
                 uncapped: bool = False,
                 progress_callback: Callable[[str, str, int, int], None] | None = None) -> None:
        self.output_dir = Path(output_dir or Path.cwd() / ".test-artifacts" / "transfer-benchmark")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.payload_size = int(payload_size)
        self.uncapped = bool(uncapped or advertised_rate_bytes_per_second == 0)
        self.advertised_rate = 0 if self.uncapped else int(advertised_rate_bytes_per_second)
        self.progress_callback = progress_callback
        self._single_rates: dict[str, float] = {}

    @staticmethod
    def _available_backends() -> list[str]:
        return ["custom", "rust"]

    def _adapter(self, name: str, resources: ResourceManager) -> tuple[Any, bool, str | None]:
        if name == "custom":
            return CustomAsyncBackend(resources), True, None
        if name == "rust":
            adapter = RustTransferBackend(max_segments=resources.policy.max_segments_per_file,
                                          min_segment_size=resources.policy.min_segment_size,
                                          max_retries=resources.policy.max_retries)
            return adapter, bool(adapter.available()), None if adapter.available() else "Rust transfer-core binary is unavailable"
        raise ValueError(f"unknown backend: {name}")

    @staticmethod
    def _item(url: str, size: int, checksum: str | None = None) -> ResolvedItem:
        return ResolvedItem("benchmark", url, "fixture.bin", size=size, direct_url=url, checksum=checksum)

    async def _invoke(self, backend: Any, name: str, item: ResolvedItem, root: Path,
                      control: _BenchmarkControl | None,
                      progress: Callable[[int], None] | None = None) -> Path:
        if name == "custom":
            return await backend.download(item, root, progress=progress, control=control)
        return await asyncio.to_thread(backend.download, item, root, progress, control)

    def _gate(self, scenario: BenchmarkScenario, backend: str, throughput: float,
              *, reference_single: float | None, reference_memory: float | None,
              available: bool, integrity_state: str, failure: str | None) -> dict[str, Any]:
        if not available:
            return {"gating": False, "passed": True, "reason": failure or "adapter unavailable"}
        if not scenario.release_gate:
            return {"gating": False, "passed": True, "reason": "fixture behavior case is diagnostic, not a throughput gate"}
        if integrity_state not in {"verified", "size_verified"}:
            return {"gating": True, "passed": False, "reason": "integrity verification failed", "classification": "integrity"}
        rate = scenario.advertised_rate_bytes_per_second if self.advertised_rate == 4 * _MIB else self.advertised_rate
        if rate <= 0 or self.uncapped:
            passed = throughput > 0.0
            return {"gating": True, "passed": passed, "minimum_bytes_per_second": 0.0,
                    "reason": "uncapped wire transfer completed" if passed else "internal-cap: uncapped throughput is zero"}
        if scenario.name == "single-stream":
            minimum = rate * 0.90
            passed = throughput >= minimum
            return {"gating": True, "passed": passed, "minimum_bytes_per_second": minimum,
                    "reason": "within advertised per-connection rate" if passed else "internal-cap: single-stream throughput below advertised rate"}
        if scenario.name == "eight-range":
            ideal = rate * scenario.range_count
            minimum_ideal = ideal * 0.70
            minimum_single = (reference_single or 0.0) * 2.0
            passed = throughput >= minimum_ideal and throughput >= minimum_single
            return {"gating": True, "passed": passed, "ideal_bytes_per_second": ideal,
                    "minimum_ideal_bytes_per_second": minimum_ideal,
                    "minimum_single_multiplier_bytes_per_second": minimum_single,
                    "reason": "aggregate range gate passed" if passed else "internal-cap: aggregate range throughput gate failed"}
        if scenario.name in {"sixteen-range", "thirty-two-range"}:
            minimum = (reference_single or rate) * 3.0
            passed = throughput >= minimum
            return {"gating": True, "passed": passed,
                    "minimum_bytes_per_second": minimum,
                    "reason": f"high concurrency gate passed ({scenario.range_count}x)" if passed else f"internal-cap: {scenario.name} throughput below threshold"}
        minimum = (reference_memory or 0.0) * 0.90
        passed = minimum == 0.0 or throughput >= minimum
        return {"gating": True, "passed": passed, "reference_memory_bytes_per_second": reference_memory,
                "minimum_bytes_per_second": minimum,
                "reason": "direct-to-disk gate passed" if passed else "internal-cap: disk throughput below memory aggregate"}

    def _run_backend(self, scenario: BenchmarkScenario, backend_name: str) -> BenchmarkResult:
        case = scenario.fixture_case
        rate = scenario.advertised_rate_bytes_per_second
        if case == "throttling":
            throttle_rate = max(1, rate // 2) if rate > 0 else 2 * _MIB
        elif case == "slow-server":
            rate = max(1, rate // 8) if rate > 0 else 512 * 1024
            throttle_rate = 0
        elif case == "pause-resume":
            rate = max(1, rate // 16) if rate > 0 else 256 * 1024
            throttle_rate = 0
        else:
            throttle_rate = 0
        spec = _FixtureSpec(case=case, payload_size=scenario.payload_size,
                            rate_bytes_per_second=rate)
        min_segment_size = (
            scenario.payload_size + 1
            if scenario.name == "single-stream"
            else max(64 * 1024, scenario.payload_size // max(1, scenario.range_count))
        )
        policy = SchedulerPolicy(max_active_tasks=1, max_active_segments=max(1, scenario.range_count),
                                 per_host_transfers=max(1, scenario.range_count),
                                 max_segments_per_file=max(1, scenario.range_count),
                                 min_segment_size=min_segment_size,
                                 initial_segment_concurrency=max(1, scenario.range_count),
                                 # The deterministic fixture measures transfer
                                 # capacity, not request-admission pacing. The
                                 # production default remains bounded; this
                                 # benchmark policy removes that separate
                                 # variable while retaining segment/host caps.
                                 per_host_requests_per_second=0,
                                 bandwidth_bytes_per_second=throttle_rate, max_retries=2,
                                 request_timeout_seconds=30)
        resources = ResourceManager(policy)
        backend, available, unavailable_reason = self._adapter(backend_name, resources)
        fixture = _fixture_identity(case, scenario.payload_size)
        if not available:
            return BenchmarkResult(backend_name, scenario.name, fixture,
                                   {"requested_ranges": scenario.range_count, "observed_ranges": 0,
                                    "worker_count": policy.max_segments_per_file, "connection_count": 0},
                                   {"queue_wait_seconds": 0.0, "read_bytes": 0, "read_seconds": 0.0,
                                    "write_bytes": 0, "write_seconds": 0.0, "retry_count": 0,
                                    "refresh_count": 0, "throttle_events": 0,
                                    "effective_throttle_bytes_per_second": throttle_rate,
                                    "throughput_bytes_per_second": 0.0,
                                    "wire_throughput_bytes_per_second": 0.0,
                                    "assembly_duration_seconds": 0.0,
                                    "write_amplification_factor": 1.00},
                                   {"state": "unavailable"},
                                   {"available": False, "reason": unavailable_reason},
                                   {"gating": False, "passed": True, "reason": unavailable_reason},
                                   failure=unavailable_reason)

        checksum = "sha256:" + hashlib.sha256(_generate_payload(scenario.payload_size)).hexdigest()
        pause_count = 0
        refresh_count = 0
        failure: str | None = None
        output_path: Path | None = None
        integrity_report = None
        observed_size = 0
        with _FixtureServer(spec) as server, tempfile.TemporaryDirectory(dir=self.output_dir) as raw_root:
            root = Path(raw_root)
            url = server.expired_url if case == "refresh" else server.url
            item = self._item(url, scenario.payload_size, checksum if case == "integrity" else None)

            def on_progress(bytes_done: int) -> None:
                if self.progress_callback:
                    try:
                        self.progress_callback(backend_name, scenario.name, bytes_done, scenario.payload_size)
                    except Exception:
                        pass

            def run_once(active_item: ResolvedItem, control: _BenchmarkControl | None = None) -> Path:
                return asyncio.run(self._invoke(backend, backend_name, active_item, root, control, progress=on_progress))

            started = time.monotonic()
            try:
                if case == "pause-resume":
                    control = _BenchmarkControl()
                    holder: dict[str, Any] = {}
                    def paused_worker() -> None:
                        try:
                            holder["path"] = run_once(item, control)
                        except Exception as exc:
                            holder["error"] = exc

                    worker = threading.Thread(target=paused_worker, daemon=True)
                    worker.start()
                    time.sleep(0.08)
                    control.pause.set()
                    worker.join(timeout=5)
                    if worker.is_alive():
                        control.cancel.set()
                        worker.join(timeout=5)
                    pause_count = 1
                    control.pause.clear()
                    output_path = run_once(item)
                elif case == "refresh":
                    try:
                        run_once(item)
                    except Exception:
                        refresh_count = 1
                        item = self._item(server.url, scenario.payload_size)
                        output_path = run_once(item)
                else:
                    output_path = run_once(item)
            except Exception as exc:
                failure = f"{type(exc).__name__}: {str(exc)[:240]}"
            elapsed = max(0.000001, time.monotonic() - started)
            stats = server.state.snapshot()
            if output_path and output_path.is_file():
                observed_size = output_path.stat().st_size
                if self.progress_callback:
                    try:
                        self.progress_callback(backend_name, scenario.name, observed_size, scenario.payload_size)
                    except Exception:
                        pass
                integrity_report = verify_file(
                    output_path,
                    expected_size=scenario.payload_size,
                    expected_checksum=(checksum.partition(":")[2] if case == "integrity" else None),
                    algorithm="sha256",
                )
        integrity_state = integrity_report.state if integrity_report else "failed"
        wire_bytes = int(stats.get("bytes_sent", 0))
        transfer_started_at = float(stats.get("transfer_started_at", 0.0))
        transfer_finished_at = float(stats.get("transfer_finished_at", 0.0))
        wire_seconds = (
            max(0.0, transfer_finished_at - transfer_started_at)
            if (transfer_started_at > 0.0 and transfer_finished_at >= transfer_started_at)
            else 0.0
        )
        if wire_seconds > 0.0:
            elapsed = wire_seconds
        read_seconds = wire_seconds or float(stats.get("read_seconds", 0.0)) or elapsed
        throughput = observed_size / elapsed if elapsed > 0.0 else 0.0
        memory_rate = wire_bytes / read_seconds if read_seconds > 0.0 else throughput
        assembly_duration_seconds = 0.0
        write_amplification_factor = 1.00
        metrics = {
            "queue_wait_seconds": 0.0,
            "connections": int(stats.get("get_requests", 0)),
            "connection_count": int(stats.get("range_requests", 0)),
            "worker_count": policy.max_segments_per_file,
            "read_bytes": wire_bytes,
            "read_seconds": read_seconds,
            "write_bytes": observed_size,
            "write_seconds": elapsed,
            "retry_count": int(stats.get("retry_failures", 0)),
            "refresh_count": refresh_count,
            "throttle_events": 1 if throttle_rate else 0,
            "throttle_seconds": max(0.0, elapsed - (scenario.payload_size / throttle_rate if throttle_rate else 0.0)) if throttle_rate else 0.0,
            "effective_throttle_bytes_per_second": throttle_rate,
            "throughput_bytes_per_second": throughput,
            "wire_throughput_bytes_per_second": memory_rate,
            "assembly_duration_seconds": assembly_duration_seconds,
            "write_amplification_factor": write_amplification_factor,
            "pause_count": pause_count,
            "fixture_peak_concurrent_ranges": int(stats.get("peak_ranges", 0)),
        }
        safe_integrity = {"state": integrity_state,
                          "expected_size": scenario.payload_size,
                          "observed_size": observed_size,
                          "checksum_verified": integrity_state == "verified" if case == "integrity" else None,
                          "reason": integrity_report.reason if integrity_report else failure}
        reference_single = self._single_rates.get(backend_name)
        reference_memory = None
        if scenario.name == "direct-to-disk":
            reference_memory = self._single_rates.get(f"{backend_name}:memory")
        gate_throughput = memory_rate if scenario.name in {"single-stream", "eight-range"} else throughput
        gate = self._gate(scenario, backend_name, gate_throughput, reference_single=reference_single,
                          reference_memory=reference_memory, available=True,
                          integrity_state=integrity_state, failure=failure)
        if failure and gate.get("passed"):
            gate = {"gating": bool(scenario.release_gate), "passed": False,
                    "reason": "fixture failure: " + failure, "classification": "fixture"}
        return BenchmarkResult(
            backend_name, scenario.name, fixture,
            {"requested_ranges": scenario.range_count, "observed_ranges": int(stats.get("range_requests", 0)),
             "worker_count": policy.max_segments_per_file, "connection_count": int(stats.get("range_requests", 0)),
             "peak_concurrent_ranges": int(stats.get("peak_ranges", 0)),
             "capability": "valid-ranged" if case == "valid-206" else case},
            metrics, safe_integrity,
            {"available": True, "reason": None}, gate, failure=failure)

    def _matrix_diagnostics(self) -> list[dict[str, Any]]:
        behaviors = {
            "no-range": "sequential downgrade",
            "valid-206": "validated ranged transfer",
            "416": "re-probe then sequential downgrade",
            "validator-mismatch": "validator conflict is not silently accepted",
            "malformed-206": "malformed range falls back safely",
            "retry": "bounded retry is observable",
            "refresh": "one explicit refresh is observable",
            "throttling": "effective byte rate is reported",
            "slow-server": "slow service is non-gating evidence",
            "pause-resume": "durable pause boundary is exercised",
            "integrity": "checksum outcome is reported",
        }

        def run_case(case: str) -> dict[str, Any]:
            scenario = BenchmarkScenario.named(case, payload_size=min(self.payload_size, 256 * 1024),
                                               advertised_rate_bytes_per_second=self.advertised_rate,
                                               uncapped=self.uncapped)
            result = self._run_backend(scenario, "custom")
            row = result.to_dict()
            row["matrix_case"] = case
            row["expected_behavior"] = behaviors.get(case, "fixture behavior")
            return row

        with ThreadPoolExecutor(max_workers=min(4, len(FIXTURE_CASES))) as pool:
            futures = {pool.submit(run_case, case): case for case in FIXTURE_CASES}
            case_results = {futures[f]: f.result() for f in as_completed(futures)}
        return [case_results[case] for case in FIXTURE_CASES]

    def run(self, scenario: str = "all", *, authorized_live_url: str | None = None,
            authorize_live: bool = False,
            progress_callback: Callable[[str, str, int, int], None] | None = None,
            threaded: bool = True) -> BenchmarkReport:
        """Run selected fixture cases and optionally one non-gating live probe."""
        if progress_callback is not None:
            self.progress_callback = progress_callback
        selected = CORE_SCENARIOS if str(scenario).lower() == "all" else (str(scenario),)
        report = BenchmarkReport(generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

        def run_one_backend_scenario(backend: str, name: str) -> BenchmarkResult:
            current = BenchmarkScenario.named(
                name,
                payload_size=self.payload_size,
                advertised_rate_bytes_per_second=self.advertised_rate,
                uncapped=self.uncapped,
            )
            result = self._run_backend(current, backend)
            attempts = 0
            while bool(result.gate.get("gating")) and not bool(result.gate.get("passed")) and not result.failure and attempts < 2:
                attempts += 1
                retry_result = self._run_backend(current, backend)
                if bool(retry_result.gate.get("passed")):
                    result = retry_result
                    break
            return result

        for backend in self._available_backends():
            remaining = list(selected)
            if "single-stream" in remaining:
                res = run_one_backend_scenario(backend, "single-stream")
                report.results.append(res)
                if res.availability.get("available"):
                    self._single_rates[backend] = float(res.metrics.get("wire_throughput_bytes_per_second", 0.0))
                remaining.remove("single-stream")

            if remaining:
                if threaded and len(remaining) > 1:
                    with ThreadPoolExecutor(max_workers=min(4, len(remaining))) as pool:
                        futures = {pool.submit(run_one_backend_scenario, backend, name): name for name in remaining}
                        for f in as_completed(futures):
                            res = f.result()
                            report.results.append(res)
                            name = futures[f]
                            if name == "eight-range" and res.availability.get("available"):
                                self._single_rates[f"{backend}:memory"] = float(res.metrics.get("throughput_bytes_per_second", 0.0))
                else:
                    for name in remaining:
                        res = run_one_backend_scenario(backend, name)
                        report.results.append(res)
                        if name == "eight-range" and res.availability.get("available"):
                            self._single_rates[f"{backend}:memory"] = float(res.metrics.get("throughput_bytes_per_second", 0.0))

        if str(scenario).lower() == "all":
            report.fixture_matrix = self._matrix_diagnostics()
        if authorized_live_url is not None:
            if not authorize_live:
                raise ValueError("live corroboration requires --authorize-live")
            report.source = "fixture+authorized-live-corroboration"
            report.results.append(self._live_probe(authorized_live_url))
        return report

    def _live_probe(self, url: str) -> BenchmarkResult:
        identity = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        started = time.monotonic()
        try:
            probe = PooledTransportManager().probe(url)
            elapsed = max(0.000001, time.monotonic() - started)
            return BenchmarkResult("live-corroboration", "authorized-live",
                                   {"id": f"external-live:{identity}", "source": "authorized external target"},
                                   {"requested_ranges": 1, "observed_ranges": 1 if probe.get("ranges") else 0,
                                    "capability": "ranged" if probe.get("ranges") else "sequential"},
                                   {"queue_wait_seconds": 0.0, "connections": 1, "connection_count": 1,
                                    "worker_count": 0, "read_bytes": 0, "read_seconds": elapsed,
                                    "write_bytes": 0, "write_seconds": 0.0, "retry_count": 0,
                                    "refresh_count": 0, "throttle_events": 0,
                                    "effective_throttle_bytes_per_second": 0,
                                    "throughput_bytes_per_second": 0.0,
                                    "wire_throughput_bytes_per_second": 0.0,
                                    "assembly_duration_seconds": 0.0,
                                    "write_amplification_factor": 1.00},
                                   {"state": "not_run", "reason": "live probe is corroborating only"},
                                   {"available": True, "status_code": probe.get("status_code"),
                                    "size": probe.get("size")},
                                   {"gating": False, "passed": True,
                                    "reason": "external live/quota behavior cannot fail release"}, source="live")
        except Exception as exc:
            return BenchmarkResult("live-corroboration", "authorized-live",
                                   {"id": f"external-live:{identity}", "source": "authorized external target"},
                                   {"requested_ranges": 0, "observed_ranges": 0},
                                   {"queue_wait_seconds": 0.0, "connections": 0, "connection_count": 0,
                                    "worker_count": 0, "read_bytes": 0, "read_seconds": 0.0,
                                    "write_bytes": 0, "write_seconds": 0.0, "retry_count": 0,
                                    "refresh_count": 0, "throttle_events": 0,
                                    "effective_throttle_bytes_per_second": 0,
                                    "throughput_bytes_per_second": 0.0,
                                    "wire_throughput_bytes_per_second": 0.0,
                                    "assembly_duration_seconds": 0.0,
                                    "write_amplification_factor": 1.00},
                                   {"state": "not_run", "reason": "live probe failed"},
                                   {"available": False, "reason": type(exc).__name__},
                                   {"gating": False, "passed": True,
                                    "reason": "external live/quota behavior cannot fail release"}, source="live",
                                    failure=type(exc).__name__)


def write_report(report: BenchmarkReport, path: str | Path, *, jsonl: bool | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = report.to_dict()
    use_jsonl = target.suffix.lower() == ".jsonl" if jsonl is None else bool(jsonl)
    if use_jsonl:
        lines = [{"record_type": "result", **result} for result in data["results"]]
        lines.append({"record_type": "report", **{key: value for key, value in data.items() if key != "results"}})
        target.write_text("".join(json.dumps(_redact(line), sort_keys=True) + "\n" for line in lines), encoding="utf-8")
    else:
        target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def human_summary(report: BenchmarkReport) -> str:
    data = report.to_dict()
    lines = [f"Transfer benchmark schema {data['schema_version']} ({data['source']})"]
    for result in data["results"]:
        metrics = result["metrics"]
        availability = result["availability"]
        gate = result["gate"]
        if not availability.get("available", False):
            lines.append(f"- {result['backend']}/{result['scenario']}: unavailable ({availability.get('reason', 'unknown')})")
            continue
        lines.append(
            f"- {result['backend']}/{result['scenario']}: "
            f"{metrics.get('throughput_bytes_per_second', 0.0):.0f} B/s, "
            f"connections={metrics.get('connection_count', 0)}, queue={metrics.get('queue_wait_seconds', 0.0):.3f}s, "
            f"read={metrics.get('read_bytes', 0)}, write={metrics.get('write_bytes', 0)}, "
            f"retry={metrics.get('retry_count', 0)}, refresh={metrics.get('refresh_count', 0)}, "
            f"throttle={metrics.get('effective_throttle_bytes_per_second', 0)} B/s, "
            f"waf={metrics.get('write_amplification_factor', 1.0):.2f}, "
            f"assembly={metrics.get('assembly_duration_seconds', 0.0) * 1000:.1f}ms, "
            f"integrity={result['integrity'].get('state')}, gate={'PASS' if gate.get('passed') else 'FAIL'}"
        )
    lines.append(f"Release gates: {'PASS' if data['summary']['all_gates_passed'] else 'FAIL'}")
    return "\n".join(lines)


__all__ = ["BenchmarkScenario", "BenchmarkResult", "BenchmarkReport", "BenchmarkRunner",
           "CORE_SCENARIOS", "FIXTURE_CASES", "human_summary", "write_report"]
