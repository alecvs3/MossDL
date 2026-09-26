"""Deterministic end-to-end lifecycle harness.

Drives the production ``EngineService`` through resolution, hoster wait timer,
CAPTCHA, transfer, integrity verification, and the archive pipeline using
scripted seams only (resolution broker, browser solver, archive backend).
No live network is used, so the matrix runs inside the normal quality gate.

Every telemetry record, engine event, stage transition, and final artifact is
captured so a failing scenario is fully explainable.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from email.utils import formatdate
from pathlib import Path
from typing import Any

from engine.errors import NeedsCaptcha
from engine.models import ResolvedItem
from engine.service import EngineService
from engine.telemetry import LogEvent, telemetry_bus
from tests.e2e.faults import (
    ProgressWatcher,
    RangeFaultPolicy,
    UnlinkFault,
    breaker_events,
    expire_breaker_cooldown,
    isolated_auditor,
    missing_volumes,
)

VALID_CLEARANCE = "clearance-valid"


@dataclass
class Scenario:
    """One lifecycle edge case."""

    name: str
    parts: int = 1
    timer_seconds: int = 0
    require_captcha: bool = True
    token_rejections: int = 0
    solver_timeout: bool = False
    checksum_mismatch: bool = False
    auto_extract: bool = True
    timeout_seconds: float = 40.0
    expect_states: tuple[str, ...] = ("completed",)
    drive_ui_captcha: bool = False
    payload_bytes: int = 256 * 1024
    expect_challenges: int | None = None
    expect_archive_completed: bool = False
    expect_timer_stage: bool = False
    share_clearance: bool = True
    solver_delay_seconds: float = 0.0
    serialize_host: bool = False

    # --- wave 5 fault injections -------------------------------------------
    ranged: bool = False
    """Serve byte ranges so the segmented (multi-chunk) transfer path runs."""
    segment_bytes: int = 256 * 1024
    """``min_segment_size`` for ranged scenarios (keeps fixtures small/fast)."""
    segment_workers: int = 1
    """``max_segments_per_file`` -- 1 makes permit-release sampling exact."""
    throttle_after_requests: int = 1
    throttle_requests: int = 0
    throttle_status: int = 429
    throttle_retry_after: float = 1.0
    missing_parts: tuple[int, ...] = ()
    """Volume numbers that never land (their task is never created)."""
    cleanup_lock_failures: int = 0
    """First N unlink attempts per source volume raise WinError 32."""
    cleanup_lock_permanent: bool = False
    breaker_recovery_tasks: int = 0
    """Extra real downloads run after the cooldown is fast-forwarded."""
    deep: bool = False
    """Opt-in scenario; only runs with TRANSFER_MANAGER_DEEP=1."""
    force_backend: str = ""
    """Restrict backend selection (the transport backpressure seams are the
    Python ``custom`` backend; automatic selection would pick ``rust``)."""


@dataclass
class ScenarioResult:
    scenario: Scenario
    task_ids: list[str] = field(default_factory=list)
    final_states: dict[str, str] = field(default_factory=dict)
    stages: dict[str, list[str]] = field(default_factory=dict)
    telemetry: list[LogEvent] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    archive_jobs: list[dict[str, Any]] = field(default_factory=list)
    destination_files: list[str] = field(default_factory=list)
    challenge_count: int = 0
    solve_count: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    artifact_dir: str = ""
    task_packages: dict[str, dict[str, Any]] = field(default_factory=dict)
    fault: dict[str, Any] = field(default_factory=dict)
    """Fault-injection evidence: throttle counts, watcher samples, breaker state."""

    def stage_sequence(self, task_id: str) -> list[str]:
        return list(self.stages.get(task_id, []))


class _TelemetryCapture:
    def __init__(self) -> None:
        self.events: list[LogEvent] = []
        self._lock = threading.Lock()

    def __call__(self, event: LogEvent) -> None:
        with self._lock:
            self.events.append(event)

    def attach(self) -> None:
        telemetry_bus.add_listener(self)

    def detach(self) -> None:
        telemetry_bus.remove_listener(self)


class _LocalFileServer:
    """Deterministic in-memory HTTP file server for direct links."""

    def __init__(self, payloads: dict[str, bytes], ranges: bool = False,
                 fault: RangeFaultPolicy | None = None) -> None:
        self._payloads = payloads
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "LifecycleHarness/1"

            def _body(self) -> bytes | None:
                return outer._payloads.get(Path(self.path.split("?", 1)[0]).name)

            def _etag(self, body: bytes) -> str:
                return f'"harness-{len(body)}-{Path(self.path).name}"'

            def _requested_range(self, body: bytes) -> tuple[int, int] | None:
                header = self.headers.get("Range", "")
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", header.strip()) if header else None
                if not match:
                    return None
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else len(body) - 1
                return start, min(end, len(body) - 1)

            def _throttle(self, span: tuple[int, int], body: bytes) -> bool:
                """Answer a range request with 429/403 + Retry-After when scripted."""
                if fault is None or span[1] - span[0] < 1:
                    return False  # the bytes 0-0 capability probe stays clean
                verdict = fault.next_range_response()
                if verdict is None:
                    return False
                status, retry_after = verdict
                self.send_response(status)
                self.send_header("Retry-After", f"{retry_after:g}")
                self.send_header("Content-Length", "0")
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                return True

            def do_HEAD(self) -> None:  # noqa: N802
                body = self._body()
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes" if ranges else "none")
                self.send_header("Content-Type", "application/octet-stream")
                if ranges:
                    self.send_header("ETag", self._etag(body))
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                body = self._body()
                if body is None:
                    self.send_error(404)
                    return
                span = self._requested_range(body) if ranges else None
                if span is not None:
                    if self._throttle(span, body):
                        return
                    chunk = body[span[0]: span[1] + 1]
                    self.send_response(206)
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Content-Range", f"bytes {span[0]}-{span[1]}/{len(body)}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("ETag", self._etag(body))
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    self._write(chunk)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes" if ranges else "none")
                self.send_header("Content-Type", "application/octet-stream")
                if ranges:
                    self.send_header("ETag", self._etag(body))
                self.send_header("Last-Modified", formatdate(time.time(), usegmt=True))
                self.end_headers()
                self._write(body)

            def _write(self, body: bytes) -> None:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return

            def log_message(self, *_args: object) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _HosterSim:
    """Scripted hoster: Turnstile until reusable clearance exists."""

    def __init__(self, service: EngineService, port: int, payloads: dict[str, bytes],
                 scenario: Scenario) -> None:
        self.service = service
        self.port = port
        self.payloads = payloads
        self.scenario = scenario
        self.rejections_done = 0
        self.solved_names: set[str] = set()
        self.solve_count = 0

    def resolve(self, url: str, secrets: dict[str, Any], *_args: Any) -> list[ResolvedItem]:
        name = Path(url).name
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        payload = self.payloads.get(name, b"")
        if self.scenario.share_clearance:
            # Reusable host clearance (what production should provide).
            session = self.service.get_host_session(host)
            clearance_ok = (session.get("cf_clearance") == VALID_CLEARANCE
                            or secrets.get("cf_clearance") == VALID_CLEARANCE)
        else:
            # Counterfactual: each file must be solved on its own.
            clearance_ok = name in self.solved_names

        if clearance_ok and self.rejections_done < self.scenario.token_rejections:
            self.rejections_done += 1
            raise NeedsCaptcha(
                f"provider rejected the solved token (attempt {self.rejections_done})",
                "turnstile",
                {"page_url": url, "url": url, "site_key": "0xHARNESS"},
            )
        if not clearance_ok and self.scenario.require_captcha:
            raise NeedsCaptcha(
                "turnstile required",
                "turnstile",
                {"page_url": url, "url": url, "site_key": "0xHARNESS"},
            )
        checksum = "sha256:" + ("0" * 64) if self.scenario.checksum_mismatch else None
        return [ResolvedItem(
            provider="datanodes",
            source_url=url,
            display_name=name,
            relative_path="",
            size=len(payload),
            direct_url=f"http://127.0.0.1:{self.port}/{name}",
            checksum=checksum,
            metadata={"type": "file"},
        )]


class _FakeArchiveBackend:
    def __init__(self) -> None:
        self.calls = 0
        self.refusals: list[dict[str, Any]] = []

    def available(self) -> bool:
        return True

    def extract(self, path: Any, output_directory: Any, password_ref: Any = None,
                policy: Any = None, control: Any = None, job_id: str | None = None) -> dict[str, Any]:
        self.calls += 1
        # A real extractor cannot join a package with a hole in it; refusing
        # here is what keeps an incomplete set from being "extracted" and then
        # having its sources deleted.
        gaps = missing_volumes(Path(path))
        if gaps:
            self.refusals.append({"input": str(path), "missing_parts": gaps})
            raise RuntimeError(
                f"cannot find volume part{gaps[0]} of {Path(path).name}; "
                f"package is missing volume(s) {gaps}")
        out = Path(output_directory)
        out.mkdir(parents=True, exist_ok=True)
        payload = b"extracted-payload-" * 4096
        (out / "content.bin").write_bytes(payload)
        if job_id:
            from engine.archive_backend import completion_sentinel_path
            completion_sentinel_path(out, job_id).write_bytes(b"")
        return {"output_directory": str(out), "files": 1, "bytes": len(payload)}

    def probe(self, path: Any, password_ref: Any = None, policy: Any = None) -> dict[str, Any]:
        return {"files": [], "encrypted": False}

    def list(self, path: Any, password_ref: Any = None, policy: Any = None) -> dict[str, Any]:
        return {"files": [], "encrypted": False}

    def test(self, path: Any, password_ref: Any = None, policy: Any = None,
             control: Any = None) -> dict[str, Any]:
        return {"ok": True}

    def call(self, method: str, params: dict[str, Any], control: Any = None) -> dict[str, Any]:
        return {}


def _emit_solver_stages(service: EngineService, task_id: str | None, scenario: Scenario,
                        *, threadsafe: bool) -> None:
    if not task_id:
        return
    emit = service._on_solver_stage_threadsafe if threadsafe else service._on_solver_stage
    if scenario.timer_seconds > 0:
        for remaining in range(scenario.timer_seconds, -1, -1):
            emit(task_id, "countdown", {"countdown_seconds": remaining})
            if threadsafe:
                time.sleep(0.01)
    emit(task_id, "turnstile_detected", {})
    emit(task_id, "turnstile_solved", {})


def _mark_solved(service: EngineService, hoster: "_HosterSim", task_id: str | None,
                 target_url: str | None) -> None:
    """Record which file a solve actually covered (used by the no-sharing mode)."""
    name = ""
    task = service.store.get(task_id) if task_id else None
    if task:
        name = Path(task.source_url).name
    if not name and target_url:
        name = Path(target_url).name
    if name:
        hoster.solved_names.add(name)


def _install_solver(service: EngineService, scenario: Scenario, hoster: "_HosterSim") -> None:
    """Patch both the async cascade and the UI-driven multipart solve."""

    async def fake_request_solution(challenge: Any, force_automated: bool = False) -> dict[str, Any]:
        import asyncio
        from engine import challenge_lifecycle

        challenge_lifecycle.advance(challenge, challenge_lifecycle.ROUTING, "harness solver selected")
        challenge_lifecycle.advance(challenge, challenge_lifecycle.SOLVING, "harness solver running")

        task_id = getattr(challenge, "task_id", None)
        hoster.solve_count += 1
        _emit_solver_stages(service, task_id, scenario, threadsafe=False)
        if scenario.solver_delay_seconds > 0:
            await asyncio.sleep(scenario.solver_delay_seconds)
        _mark_solved(service, hoster, task_id, None)
        if scenario.solver_timeout:
            raise TimeoutError("harness solver timed out")
        solution = {
            "token": "harness-token",
            "turnstile_token": "harness-token",
            "cf-turnstile-response": "harness-token",
            "cf_clearance": VALID_CLEARANCE,
            "cookies": {"cf_clearance": VALID_CLEARANCE, "__cf_bm": "harness"},
            "user_agent": "harness-agent",
            "engine": "FakeClearcote",
        }
        if not service.captcha.solve_challenge(challenge.id, solution, "harness"):
            raise AssertionError("Harness answer was not accepted for verification")
        return solution

    service.captcha.request_solution = fake_request_solution  # type: ignore[assignment]

    from engine.browser_solver import solver_daemon

    def fake_solve_challenge_sync(target_url: str, timeout: float = 35.0,
                                  cookies: Any = None, task_id: str | None = None) -> dict[str, Any]:
        hoster.solve_count += 1
        _emit_solver_stages(service, task_id, scenario, threadsafe=True)
        if scenario.solver_delay_seconds > 0:
            time.sleep(scenario.solver_delay_seconds)
        _mark_solved(service, hoster, task_id, target_url)
        if scenario.solver_timeout:
            return {"success": False, "error": "harness solver timed out"}
        return {
            "success": True,
            "turnstile_token": "harness-token",
            "cookies": {"cf_clearance": VALID_CLEARANCE, "__cf_bm": "harness"},
            "user_agent": "harness-agent",
            "engine": "FakeClearcote",
        }

    solver_daemon.solve_challenge_sync = fake_solve_challenge_sync  # type: ignore[assignment]


HARNESS_HOST = "127.0.0.1"


def _fixtures(scenario: Scenario) -> tuple[list[str], dict[str, bytes]]:
    """Fixture names to enqueue plus every payload the server will offer."""
    names: list[str] = []
    payloads: dict[str, bytes] = {}
    for index in range(1, scenario.parts + 1):
        if scenario.parts > 1:
            name = f"Fixture_v1--_example-repacks.test_--_.part{index}.rar"
        else:
            name = "Fixture_v1--_example-repacks.test_--_.rar"
        if index in scenario.missing_parts:
            # The volume that never lands: no task, no payload, no bytes.
            continue
        names.append(name)
        payloads[name] = bytes([index or 1]) * scenario.payload_bytes
    for index in range(1, scenario.breaker_recovery_tasks + 1):
        name = f"Recovery_{index}.bin"
        payloads[name] = bytes([0xAB]) * scenario.payload_bytes
    return names, payloads


def _tune_policy(service: EngineService, scenario: Scenario) -> None:
    service.resources.policy.max_retries = 2
    service.storage_concurrency.set_limit(
        HARNESS_HOST, 1 if scenario.serialize_host else 4, calibrated=True)
    if scenario.force_backend:
        only = {scenario.force_backend: getattr(service, f"{scenario.force_backend}_backend")}
        service._transfer_backends = lambda: dict(only)  # type: ignore[assignment]
    if not scenario.ranged:
        return
    # Small ranges keep the fixture tiny while still exercising the real
    # segmented transfer path (one worker makes permit sampling unambiguous).
    service.resources.policy.min_segment_size = scenario.segment_bytes
    service.resources.policy.max_segments_per_file = scenario.segment_workers
    service.resources.policy.initial_segment_concurrency = scenario.segment_workers


def _add_task(service: EngineService, scenario: Scenario, destination: Path, name: str) -> str:
    created = service.dispatch("add_task", {
        "url": f"https://datanodes.to/harness/{name}",
        "destination": str(destination),
        "display_name": name,
        "folder_path": str(destination),
        "browser_context": {"auto_extract": scenario.auto_extract},
    })
    return str(created["id"])


def _await_tasks(service: EngineService, task_ids: list[str], scenario: Scenario,
                 timeout_seconds: float, solved: set[str]) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        tasks = [service.store.get(tid) for tid in task_ids]
        if all(t is not None and t.state in {"completed", "failed", "canceled"} for t in tasks):
            return
        if scenario.drive_ui_captcha:
            for task in tasks:
                if not task or task.state != "needs_user":
                    continue
                challenge = task.user_challenge or {}
                cid = challenge.get("challenge_id") or challenge.get("id")
                if cid and cid not in solved:
                    solved.add(cid)
                    service.dispatch("captcha_solve", {"challenge_id": cid})
        time.sleep(0.2)


def _run_breaker_recovery(service: EngineService, scenario: Scenario,
                          destination: Path) -> dict[str, Any]:
    """Fast-forward the breaker cooldown, then run real downloads through it.

    The breaker must walk FROZEN -> PROBATION -> CLOSED off the back of real
    transfers; only the >= 300 s cooldown is simulated.
    """
    from engine.concurrency_auditor import concurrency_auditor

    before = concurrency_auditor.breaker_snapshot(HARNESS_HOST)
    skipped = expire_breaker_cooldown(HARNESS_HOST)
    recovery_ids = [
        _add_task(service, scenario, destination, f"Recovery_{index}.bin")
        for index in range(1, scenario.breaker_recovery_tasks + 1)
    ]
    _await_tasks(service, recovery_ids, scenario, 30.0, set())
    after = concurrency_auditor.breaker_snapshot(HARNESS_HOST)
    return {
        "before": before,
        "after": after,
        "cooldown_seconds_skipped": skipped,
        "recovery_task_states": {
            tid: (service.store.get(tid).state if service.store.get(tid) else "missing")
            for tid in recovery_ids
        },
    }


def run_scenario(scenario: Scenario, workdir: Path, artifact_root: Path | None = None) -> ScenarioResult:
    workdir = Path(workdir)
    destination = workdir / "downloads"
    destination.mkdir(parents=True, exist_ok=True)
    data_dir = workdir / "engine"
    data_dir.mkdir(parents=True, exist_ok=True)

    names, payloads = _fixtures(scenario)
    throttle = RangeFaultPolicy(
        after_requests=scenario.throttle_after_requests,
        faults=scenario.throttle_requests,
        status=scenario.throttle_status,
        retry_after=scenario.throttle_retry_after,
    ) if scenario.throttle_requests else None
    unlink_fault = UnlinkFault(
        match=lambda path: path.suffix.lower() == ".rar" and path.parent == destination,
        failures=scenario.cleanup_lock_failures,
        permanent=scenario.cleanup_lock_permanent,
    ) if (scenario.cleanup_lock_failures or scenario.cleanup_lock_permanent) else None
    # Every scenario runs against a scenario-local auditor: the singleton
    # otherwise writes host profiles into the repository's logs/ directory and
    # would carry a tripped 127.0.0.1 breaker into the next scenario.
    tracks_breaker = bool(scenario.throttle_requests or scenario.breaker_recovery_tasks)

    server = _LocalFileServer(payloads, ranges=scenario.ranged, fault=throttle)
    capture = _TelemetryCapture()
    capture.attach()
    service: EngineService | None = None
    watcher: ProgressWatcher | None = None
    result = ScenarioResult(scenario=scenario)
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(isolated_auditor(workdir / "auditor", HARNESS_HOST))
        if unlink_fault is not None:
            stack.enter_context(unlink_fault.installed())
        service = EngineService(data_dir)
        hoster = _HosterSim(service, server.port, payloads, scenario)
        service.plugins.resolve_chain = hoster.resolve  # type: ignore[assignment]
        service.resolution_broker.resolve = lambda context, resolver: resolver(context.source_url)  # type: ignore[assignment]
        archive_backend = _FakeArchiveBackend()
        service.archive_backend = archive_backend  # type: ignore[assignment]
        _tune_policy(service, scenario)
        _install_solver(service, scenario, hoster)
        if scenario.ranged:
            watcher = ProgressWatcher(destination, service.resources, HARNESS_HOST)
            watcher.start()

        started = time.monotonic()
        for name in names:
            result.task_ids.append(_add_task(service, scenario, destination, name))
        service.dispatch("resume_engine")

        _await_tasks(service, result.task_ids, scenario, scenario.timeout_seconds, set())

        # Time-to-all-parts-complete (excludes post-download archive work).
        result.elapsed_seconds = time.monotonic() - started
        result.solve_count = hoster.solve_count
        if watcher is not None:
            watcher.stop()
        if scenario.breaker_recovery_tasks:
            result.fault["breaker_recovery"] = _run_breaker_recovery(
                service, scenario, destination)

        # Archive jobs run asynchronously after the final part completes;
        # wait for them to settle before inspecting cleanup/output.
        if scenario.auto_extract and scenario.parts > 1:
            settle_deadline = time.time() + 20.0
            while time.time() < settle_deadline:
                jobs = service.store.list_archive_jobs()
                if jobs and all(
                    j.get("state") in {"completed", "failed", "canceled", "needs_user"} for j in jobs
                ):
                    break
                time.sleep(0.2)

        for tid in result.task_ids:
            task = service.store.get(tid)
            if not task:
                continue
            result.final_states[tid] = task.state
            result.stages[tid] = [entry.get("to") for entry in (task.stage_history or []) if entry.get("to")]
            result.task_packages[tid] = {
                "package_key": task.package_key,
                "part": task.package_part_number,
                "leader": task.package_leader_id,
                "size": task.size,
            }
        result.events = [e for e in service.events.events_since(0, 5000)]
        result.archive_jobs = service.store.list_archive_jobs()
        result.destination_files = sorted(p.name for p in destination.iterdir()) if destination.exists() else []
        result.fault["destination_sizes"] = {
            p.name: p.stat().st_size for p in destination.iterdir() if p.is_file()
        } if destination.exists() else {}
        if throttle is not None:
            result.fault["throttle"] = throttle.summary()
            result.fault["throttle_windows"] = [
                {"at": round(at, 4), "retry_after": retry} for at, retry in throttle.windows]
        if unlink_fault is not None:
            result.fault["cleanup_lock"] = unlink_fault.summary()
        if archive_backend.refusals:
            result.fault["archive_refusals"] = archive_backend.refusals
        result.fault["archive_extract_calls"] = archive_backend.calls
    finally:
        if watcher is not None:
            watcher.stop()
        capture.detach()
        if service is not None:
            service.close()
        server.close()
        stack.close()

    if watcher is not None:
        result.fault["watcher"] = watcher.summary()
        result.fault["part_deletions"] = watcher.part_deletions
        result.fault["permits_released_during_backoff"] = [
            {"at": round(at, 4), "retry_after": retry,
             "released": watcher.permits_released_during(at, retry)}
            for at, retry in (throttle.windows if throttle is not None else [])
        ]
    result.telemetry = list(capture.events)
    if tracks_breaker:
        result.fault["breaker_events"] = breaker_events(capture.events, HARNESS_HOST)
    result.challenge_count = len([
        e for e in capture.events if "[CAPTCHA_CHALLENGE]" in (e.message or "")
    ])
    result.errors = [
        e.message for e in capture.events
        if (e.level or "").upper() in {"ERROR", "FATAL"} and "KeyboardInterrupt" not in (e.message or "")
    ]

    if artifact_root is not None:
        artifact = Path(artifact_root) / scenario.name
        artifact.mkdir(parents=True, exist_ok=True)
        result.artifact_dir = str(artifact)
        timeline = [
            {"timestamp": e.timestamp, "level": e.level, "subsystem": e.subsystem,
             "message": e.message, "context": e.context}
            for e in capture.events
        ]
        (artifact / "telemetry.jsonl").write_text(
            "\n".join(json.dumps(row, sort_keys=True, default=str) for row in timeline) + "\n",
            encoding="utf-8")
        (artifact / "summary.json").write_text(json.dumps({
            "scenario": scenario.name,
            "final_states": result.final_states,
            "stages": result.stages,
            "task_packages": result.task_packages,
            "challenge_count": result.challenge_count,
            "solve_count": result.solve_count,
            "elapsed_seconds": round(result.elapsed_seconds, 3),
            "archive_jobs": result.archive_jobs,
            "destination_files": result.destination_files,
            "errors": result.errors,
            "fault": result.fault,
        }, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return result


def default_scenarios() -> list[Scenario]:
    """The canonical edge-case matrix, shared by the tests and the CLI runner."""
    return [
        Scenario(
            name="single_file_timer_captcha",
            parts=1, timer_seconds=3, require_captcha=True,
            expect_challenges=1, expect_timer_stage=True,
        ),
        Scenario(
            name="multipart_shared_captcha",
            parts=4, timer_seconds=2, require_captcha=True, drive_ui_captcha=True,
            expect_challenges=1, expect_archive_completed=True,
        ),
        Scenario(
            name="token_rejected_once",
            parts=1, require_captcha=True, token_rejections=1, timer_seconds=1,
        ),
        Scenario(
            name="solver_timeout",
            parts=1, timer_seconds=1, solver_timeout=True, timeout_seconds=20.0,
            expect_states=("needs_user", "queued", "resolving", "retrying"),
        ),
        Scenario(
            name="checksum_mismatch",
            parts=1, timer_seconds=1, checksum_mismatch=True, timeout_seconds=35.0,
            expect_states=("failed",),
        ),
    ]


def _throttle_failures(scenario: Scenario, result: ScenarioResult) -> list[str]:
    failures: list[str] = []
    throttle = result.fault.get("throttle", {})
    if int(throttle.get("faults_injected", 0)) != scenario.throttle_requests:
        failures.append(
            f"throttle never fired as scripted: {throttle}; the scenario proves nothing")
    watcher = result.fault.get("watcher", {})
    if not any(int(size) > 0 for size in (watcher.get("max_part_bytes") or {}).values()):
        failures.append(f"no .part file was ever observed with bytes: {watcher}")
    deletions = result.fault.get("part_deletions") or []
    if deletions:
        failures.append(f".part file deleted during throttling (progress lost): {deletions}")
    held = [w for w in result.fault.get("permits_released_during_backoff", []) if not w["released"]]
    if held:
        failures.append(f"transfer permits were held across the Retry-After backoff: {held}")
    events = result.fault.get("breaker_events") or []
    if "BREAKER_TRIPPED" not in events:
        failures.append(f"host pressure breaker never tripped on 429/Retry-After: {events}")
    expected_bytes = scenario.payload_bytes
    landed = [name for name, size in (result.fault.get("destination_sizes") or {}).items()
              if size == expected_bytes and not name.endswith(".part")]
    if not landed:
        failures.append(
            f"no complete file landed ({expected_bytes} bytes expected): "
            f"{result.fault.get('destination_sizes')}")
    return failures


def _breaker_recovery_failures(result: ScenarioResult) -> list[str]:
    recovery = result.fault.get("breaker_recovery") or {}
    after = recovery.get("after") or {}
    failures: list[str] = []
    unfinished = {tid: state for tid, state in (recovery.get("recovery_task_states") or {}).items()
                  if state != "completed"}
    if unfinished:
        failures.append(f"recovery downloads did not complete: {unfinished}")
    if after.get("state") != "closed":
        failures.append(
            f"breaker did not walk FROZEN -> PROBATION -> CLOSED after the cooldown; "
            f"state={after.get('state')} snapshot={after}")
    if int(after.get("verified_ceiling", 0) or 0) <= 1:
        failures.append(f"host ceiling stayed pinned at {after.get('verified_ceiling')} after recovery")
    return failures


def _missing_volume_failures(scenario: Scenario, result: ScenarioResult) -> list[str]:
    """Zero deletions is the assertion that matters most: silent data loss."""
    failures: list[str] = []
    volumes = [name for name in result.destination_files if name.endswith(".rar")]
    expected = scenario.parts - len(scenario.missing_parts)
    if len(volumes) != expected:
        failures.append(
            f"source volumes were deleted for an incomplete package: kept {volumes}, "
            f"expected {expected} volume(s) -- this is silent data loss")
    completed = [j for j in result.archive_jobs if j.get("state") == "completed"]
    if completed:
        failures.append(f"archive job reported success for an incomplete package: {completed}")
    truthful = [j for j in result.archive_jobs if j.get("state") in {"failed", "needs_user"}]
    described = [e.message for e in result.telemetry
                 if any(marker in (e.message or "").lower()
                        for marker in ("missing volume", "incomplete package", "package is incomplete",
                                       "cannot find volume", "missing part"))]
    if not truthful and not described:
        failures.append(
            "incomplete package produced no failed/needs_user archive job and no telemetry "
            f"naming the missing volume; archive_jobs={result.archive_jobs}")
    return failures


def _cleanup_lock_failures(scenario: Scenario, result: ScenarioResult) -> list[str]:
    failures: list[str] = []
    lock = result.fault.get("cleanup_lock", {})
    if int(lock.get("lock_failures_injected", 0)) <= 0:
        failures.append(f"no WinError 32 was injected; the scenario proves nothing: {lock}")
    volumes = [name for name in result.destination_files if name.endswith(".rar")]
    completed = [j for j in result.archive_jobs if j.get("state") == "completed"]
    blocked = [e.message for e in result.telemetry
               if "[ARCHIVE_CLEANUP_INCOMPLETE]" in (e.message or "")
               or "[ARCHIVE_DELETE_FAILED]" in (e.message or "")
               or "[ARCHIVE_CLEANUP_BLOCKED]" in (e.message or "")]
    if scenario.cleanup_lock_permanent:
        if completed:
            failures.append(
                f"archive job claimed success while sources were still locked: {completed}")
        if len(volumes) != scenario.parts:
            failures.append(f"locked sources vanished: kept {volumes}")
        if not blocked:
            failures.append("permanently locked cleanup produced no structured telemetry")
        return failures
    if not completed:
        failures.append(
            f"archive job did not complete after a transient lock; jobs={result.archive_jobs}")
    if volumes:
        failures.append(
            f"source volumes survived a transient lock (delete did not retry): {volumes}")
    return failures


def fault_failures(scenario: Scenario, result: ScenarioResult) -> list[str]:
    """Verify one fault scenario; shared by the unittest and the CLI runner."""
    failures: list[str] = []
    if scenario.throttle_requests and scenario.throttle_status == 429:
        failures.extend(_throttle_failures(scenario, result))
    if scenario.breaker_recovery_tasks:
        failures.extend(_breaker_recovery_failures(result))
    if scenario.missing_parts:
        failures.extend(_missing_volume_failures(scenario, result))
    if scenario.cleanup_lock_failures or scenario.cleanup_lock_permanent:
        failures.extend(_cleanup_lock_failures(scenario, result))
    return failures


def deep_enabled() -> bool:
    """``TRANSFER_MANAGER_DEEP=1`` opts into the slower fault scenarios."""
    return str(os.environ.get("TRANSFER_MANAGER_DEEP", "")).strip().lower() in {"1", "true", "yes", "on"}


def fault_scenarios() -> list[Scenario]:
    """Wave-5 fault injections: throttle, missing volume, cleanup lock."""
    return [
        # 1. 429 + Retry-After on live range requests.
        Scenario(
            name="throttle_429_retry_after",
            parts=1, require_captcha=False, timer_seconds=0, auto_extract=False,
            ranged=True, payload_bytes=2 * 1024 * 1024, segment_bytes=256 * 1024,
            throttle_after_requests=1, throttle_requests=2,
            throttle_status=429, throttle_retry_after=1.0,
            breaker_recovery_tasks=3, timeout_seconds=60.0,
            force_backend="custom",
        ),
        # 2. A package volume that never lands; extraction must refuse loudly
        #    and delete nothing.
        Scenario(
            name="missing_archive_volume",
            parts=4, missing_parts=(3,), require_captcha=False, timer_seconds=0,
            timeout_seconds=60.0, expect_archive_completed=False,
        ),
        # 3. Transient WinError 32 on source cleanup; the delete must retry.
        Scenario(
            name="cleanup_transient_lock",
            parts=2, require_captcha=False, timer_seconds=0,
            cleanup_lock_failures=1, timeout_seconds=60.0,
            expect_archive_completed=True,
        ),
        # 3b. A lock that never clears: the job must NOT claim success.
        Scenario(
            name="cleanup_permanent_lock",
            parts=2, require_captcha=False, timer_seconds=0,
            cleanup_lock_permanent=True, timeout_seconds=60.0, deep=True,
        ),
        # 1b. 403 throttle variant (same transport seam, different status).
        # NOTE: kept deep-only because a 403 is classified terminal, not
        # throttle, unless the body carries throttle/HTML markers.
        Scenario(
            name="throttle_403_backpressure",
            parts=1, require_captcha=False, timer_seconds=0, auto_extract=False,
            ranged=True, payload_bytes=2 * 1024 * 1024, segment_bytes=256 * 1024,
            throttle_after_requests=1, throttle_requests=1,
            throttle_status=403, throttle_retry_after=1.0,
            timeout_seconds=60.0, deep=True, force_backend="custom",
        ),
    ]

