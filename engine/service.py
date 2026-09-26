from __future__ import annotations

import functools
import random
import re

import json
import hashlib
import hmac
import secrets as std_secrets
import sys
import threading
import asyncio
import time
import shutil
import subprocess
import os
import urllib.error
from uuid import uuid4
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .db import RevisionConflict, TaskStore
from .archive_backend import ArchiveBackend, completion_sentinel_path
from .errors import (DownloadCanceled, DownloadPaused, NeedsCaptcha, NeedsUser, QuotaExceeded,
                     ProviderMappedError, ProviderUnavailable, classify_provider_failure)
from .captcha import CaptchaChallenge, CaptchaManager, CaptchaStatus, CaptchaType, solution_token
from .archive_joiner import ArchiveOrchestrator, BinaryPartJoiner, MultiPartDetector, produced_file_manifest
from .shortlink_resolver import RecursiveShortlinkResolver
from .flaresolverr_client import FlareSolverrClient
from .custom_downloader import CustomAsyncBackend
from .limits import ResourceManager, SchedulerPolicy, normalize_bandwidth_rate
from .concurrency_auditor import concurrency_auditor
from .storage_concurrency import StorageHostConcurrencyManager
from .rust_backend import RustTransferBackend
from .models import ArchiveJob, DownloadTask, TransferLease
from .hashing import file_digest
from .events import EventPublisher
from .plugins import PluginRegistry
from .scheduler import FairAsyncScheduler
from .paths import effective_directory, validate_directory
from .routes import RouteManager
from . import challenge_lifecycle, route_http
from .wg_tunnels import TunnelSupervisor
from .fallback import ProviderFallback
from . import activity_history
from . import intake_analysis
from .browser_bridge import validate_capture
from .capture import canonical_source, normalize_candidate, rank_candidates, public_candidate, source_fingerprint
from .collection import CollectionPlanner, redact, public_url
from .container_import import normalize_import, records_as_graph
from .media_pipeline import MediaAssembler, parse_media
from .helper_sidecar import HelperSupervisor
from .models import CaptureBatch, CaptureCandidate, CollectionPlan, MediaPlan, MediaSegment, ResolutionContext, ResolvedItem
from .protocols import resolve_protocol
from .provider_health import ProviderHealthMonitor
from .provider_sdk import generate_fixture_bundle, run_contract_suite, sign_catalog, verify_catalog
from .resolution import ResolutionBroker, SessionBroker, public_item
from .resolve_stagger import ResolveStaggerController
from .universal_resolver import UniversalResolver
from .notifications import deliver
from .linkgrabber import ClipboardWatcher, extract_urls, new_link
from .autostart import get_user_autostart, set_user_autostart
from .reliability import FailureClass, RetryPolicy, classify_failure, decide_retry, verify_file
from .download_logger import download_loggers
from .backend_selection import BackendSelector, capability_snapshot
from . import transport_fallback
from .transport_metrics import TransportMetricsSink
from .file_classifier import category_for_item
from .secrets import SecretManager
from .platform import API_VERSION, api_description, request_hash, worker_hello
from .workflows import make_job, transition as workflow_transition
from .telemetry import telemetry_bus, install_telemetry_hooks
from . import critical_trace
from . import lifecycle
from .lifecycle import TaskStage
from . import title_scorer as _title_scorer


import logging

logger = logging.getLogger(__name__)

_DIAGNOSTIC_SECRET_KEYS = {"authorization", "cookie", "cookies", "password", "token", "secret",
                           "signature", "signed_url", "credential", "credential_ref"}
_API_SCOPES = {"read", "enqueue", "account_admin", "diagnostics", "provider_admin", "worker_control"}
_TERMINAL_TASK_STATES = {"completed", "canceled"}
_ACTIVATABLE_TASK_STATES = {"queued", "paused", "needs_user"}
# Integrity states that count as "good enough to continue". `verified` means a
# checksum proved the content; `size_verified` means only the length was checked
# because the provider published no checksum. They are deliberately distinct so
# nothing claims content proof it never had.
_INTEGRITY_PASS_STATES = {"verified", "size_verified"}
# Upper bound on the multipart payload-admission barrier. The barrier exists to
# stop one part consuming its direct link before a sibling has minted one, but it
# must never be able to hang a package indefinitely.
_MULTIPART_ADMISSION_TIMEOUT_SECONDS = 120.0
# Archive job states that do NOT block a fresh attempt. Anything else -- queued,
# running, extracting, cleanup_pending, completed -- means the package is either
# finished or still in flight, so leave it alone.
_ARCHIVE_RETRYABLE_STATES = {"failed", "canceled"}
# Concurrent tasks allowed by default. This is the OUTER cap; the per-host
# ceiling is meant to be the binding constraint. At the old value of 3 it was the
# other way round, so a nine-part package could never use a host's full
# allowance no matter what the ceiling had learned. Users can set 1-64.
_DEFAULT_MAX_CONCURRENT = 6
_CANARY_SETTING = "transfer_canary_policy"
_CANARY_SCOPES = {"operation", "session", "global"}
_CANARY_BACKENDS = {"custom", "rust"}
_CANARY_DEFAULT_FAILURE_CLASSES = {
    "transient_network", "rate_limited", "invalid_content", "unsupported", "unknown",
}
_CAPTURE_EXTENSION_SCHEMES = {"chrome-extension", "edge-extension", "moz-extension"}
_CAPTURE_PAGE_SCHEMES = {"http", "https"}
_CAPTURE_MAX_CANDIDATES = 256
_CAPTURE_MAX_HEADER_COUNT = 32
_CAPTURE_MAX_HEADER_BYTES = 16 * 1024

# Part 1 has cleared the challenge gauntlet only once it leaves resolution.
# Sibling lanes must never probe while Part 1 is still resolving/preflight.
_MULTIPART_PART1_RELEASED_STATES = {"downloading", "verifying", "postprocessing", "completed"}
# Single calibration point for staggered sibling probe admission (PIPE-04).
# A future calibration feed can override these without touching _pump logic.
_MULTIPART_PROBE_STAGGER_SECONDS = 0.5
_MULTIPART_PROBE_STAGGER_COLD_MULTIPLIER = 2.4
_MULTIPART_PROBE_STAGGER_JITTER_RATIO = 0.125

_HEADLESS_OPERATION_SCOPES = {
    "browser_capture_batch": "read", "capture_batch": "read", "capture_candidates": "read",
    "browser_capture": "read", "capture_review_list": "read", "capture_inbox_list": "read",
    "capture_review_get": "read", "capture_batch_get": "read", "capture_import": "enqueue",
    "capture_review_import": "enqueue", "browser_import": "enqueue",
    "crawl_get": "read", "crawl_snapshot": "read", "crawl_list": "read", "crawl_status": "read",
    "crawl_events": "read", "container_import": "enqueue", "import_container": "enqueue", "crawl_import": "enqueue",
    "media_plan": "enqueue", "media_assemble": "enqueue", "media_metadata": "read",
    "helper_invoke": "worker_control", "helper_status": "worker_control", "helper_cancel": "worker_control",
    "archive_inspect": "read", "archive_submit": "enqueue", "archive_status": "read",
    "archive_retry": "enqueue", "archive_cancel": "enqueue", "archive_extract_job": "enqueue",
    "schedule_get": "read", "schedule_set": "account_admin", "queue_pause": "account_admin", "queue_resume": "account_admin",
    "collection_select": "enqueue", "collection_enqueue": "enqueue", "task_cancel": "enqueue",
    "events_since": "read", "event_acknowledge": "read", "list_events": "read", "ack_event": "read",
    "api_info": "read", "ui_snapshot": "read", "task_status": "read", "list_archive_jobs": "read",
    "add_task": "enqueue", "download_task": "enqueue", "retry_task": "enqueue",
    "client_register": "account_admin", "client_list": "account_admin", "client_revoke": "account_admin",
    "provider_health_snapshot": "read", "provider_admission": "read", "migration_status": "diagnostics",
    "operational_report": "diagnostics", "crawl_page_matrix": "read",
    "shortlink_resolve": "read", "shortlink_resolve_recursive": "read",
    "logs_query": "read", "logs_clear": "account_admin", "logs_export": "diagnostics",
    "log_event": "read",
}

_HEADLESS_METHOD_ALIASES = {
    "archive_inspect": "archive_detect_package", "archive_submit": "archive_enqueue_extract",
    "archive_status": "list_archive_jobs", "archive_retry": "retry_archive_job",
    "archive_cancel": "cancel_archive_job", "events_since": "list_events",
    "event_acknowledge": "ack_event", "task_cancel": "cancel_task",
    "schedule_get": "list_queues", "schedule_set": "save_queue",
    "queue_pause": "pause_queue", "queue_resume": "resume_queue",
    "container_import": "container_import", "import_container": "container_import",
    "crawl_import": "container_import",
}
_HEADLESS_MUTATIONS = {
    "container_import", "import_container", "crawl_import",
    "media_plan", "media_assemble",
    "helper_invoke", "helper_cancel",
    "archive_submit", "archive_retry", "archive_cancel", "archive_extract_job",
    "schedule_set", "queue_pause", "queue_resume",
    "collection_select", "collection_enqueue",
    "task_cancel",
    "add_task", "download_task", "retry_task",
    "client_register", "client_revoke",
    "capture_import", "capture_review_import", "browser_import",
}
_HEADLESS_FORBIDDEN_KEYS = {
    "authorization", "cookie", "cookies", "password", "secret", "secrets", "token", "direct_url",
    "completed_bytes", "lease_id", "state", "task_state", "engine_state",
}


def headless_required_scope(method: str) -> str | None:
    """Return a scope only for the explicit, supported headless method matrix."""
    return _HEADLESS_OPERATION_SCOPES.get(str(method))


def authenticate_headless_token(service: "EngineService", token: str | None,
                                bootstrap_token: str | None = None) -> dict[str, Any] | None:
    presented = str(token or "")
    if not presented:
        return None
    client = service.store.find_api_client_by_hash(hashlib.sha256(presented.encode("utf-8")).hexdigest())
    if client:
        return {"client_id": str(client["id"]), "scopes": set(client.get("scopes", []))}
    if bootstrap_token and hmac.compare_digest(presented, bootstrap_token):
        return {"client_id": "bootstrap", "scopes": set(_API_SCOPES)}
    return None



def pre_dispatch(service: Any, request: Any = None,
                 auth_context: Any = None, *extra: Any, **kwargs: Any) -> Any:
    """Authenticate, authorize, validate, and replay one headless request before dispatch."""
    # Handle pre_dispatch(method, params, auth_context)
    if isinstance(service, str):
        method = service
        params = request if isinstance(request, dict) else {}
        auth = auth_context if isinstance(auth_context, dict) else {}
        actual_service = auth.get("service") or kwargs.get("service")
        if actual_service is None:
            raise ValueError("service instance is required for pre_dispatch")
        req = {"jsonrpc": "2.0", "method": method, "params": params,
               "client_id": auth.get("client_id"),
               "idempotency_key": kwargs.get("idempotency_key") or params.get("idempotency_key")}
        return pre_dispatch(actual_service, req, auth)

    # Handle pre_dispatch(service, method, params, auth_context)
    if isinstance(request, str):
        method = request
        params = auth_context if isinstance(auth_context, dict) else {}
        auth = extra[0] if extra and isinstance(extra[0], dict) else kwargs.get("auth_context", {})
        req = {"jsonrpc": "2.0", "method": method, "params": params,
               "client_id": auth.get("client_id") if isinstance(auth, dict) else None,
               "idempotency_key": kwargs.get("idempotency_key") or params.get("idempotency_key")}
        return pre_dispatch(service, req, auth)

    if not isinstance(request, dict) or request.get("jsonrpc", "2.0") != "2.0":
        raise ValueError("jsonrpc 2.0 envelope is required")
    method = str(request.get("method") or "")
    if not method:
        raise ValueError("method is required")
    required = headless_required_scope(method)
    if required is None:
        raise ValueError(f"unknown method: {method}")
    if not auth_context or not isinstance(auth_context, dict):
        raise PermissionError("unauthorized: missing authentication context")
    scopes = set(auth_context.get("scopes", []))
    if required not in scopes:
        raise PermissionError(f"insufficient scope: required {required}")
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    def forbidden_keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            found = {str(key).casefold() for key in value} & _HEADLESS_FORBIDDEN_KEYS
            for child in value.values():
                found.update(forbidden_keys(child))
            return found
        if isinstance(value, list):
            found: set[str] = set()
            for child in value:
                found.update(forbidden_keys(child))
            return found
        return set()

    forbidden = sorted(forbidden_keys(params))
    if forbidden:
        raise ValueError("operation-scoped credentials and engine state are not accepted at the RPC boundary")
    client_id = str(request.get("client_id") or auth_context.get("client_id") or "")
    if not client_id or client_id != str(auth_context.get("client_id") or ""):
        raise PermissionError("client identity does not match authenticated session")
    target = _HEADLESS_METHOD_ALIASES.get(method, method)
    dispatch_params = dict(params)
    envelope_idem = request.get("idempotency_key")
    param_idem = dispatch_params.pop("idempotency_key", None)
    idem = str(envelope_idem or param_idem or "")
    if envelope_idem and param_idem and str(envelope_idem) != str(param_idem):
        raise ValueError("idempotency keys do not match")
    if method in _HEADLESS_MUTATIONS and not idem:
        raise ValueError("mutating headless operations require idempotency_key")
    if not idem:
        return _redact_diagnostic(service.dispatch(target, dispatch_params))
    key = f"{client_id}:{idem}"
    payload_hash = request_hash({"method": method, "params": dispatch_params})
    existing = service.store.get_idempotency_record(key, method)
    if existing:
        if existing["request_hash"] != payload_hash:
            raise ValueError("idempotency key was reused with different parameters")
        return json.loads(existing["response_json"])
    result = _redact_diagnostic(service.dispatch(target, dispatch_params))
    service.store.save_idempotent(key, method, payload_hash, result)
    return result



def _redact_diagnostic(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): ("[redacted]" if any(part in str(key).lower() for part in _DIAGNOSTIC_SECRET_KEYS)
                           else _redact_diagnostic(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_diagnostic(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            parsed = urlsplit(value)
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return value[:1000]
    return value


def _normalize_input_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif "://" not in url:
        url = "https://" + url
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https", "ftp"} or not parsed.hostname:
        raise ValueError("download URL must include a valid HTTP, HTTPS, or FTP host")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials must use a credential reference")
    return url


class _TaskControl:
    def __init__(self) -> None:
        self.pause = threading.Event()
        self.cancel = threading.Event()


class EngineService:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        install_telemetry_hooks()
        telemetry_bus.set_log_file(self.data_dir / "logs" / "engine.jsonl")
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:boot",
            message="EngineService initializing",
            context={"data_dir": str(self.data_dir)},
        )
        self.store = TaskStore(self.data_dir / "downloads.sqlite3")
        concurrency_auditor.attach_store(self.store)
        concurrency_auditor.load_persisted_profiles()

        if self.store.get_setting("download_directory") is None:
            self.store.set_setting("download_directory", effective_directory("downloads", self.data_dir, create=True))
        if self.store.get_setting("archive_directory") is None:
            self.store.set_setting("archive_directory", effective_directory("archives", self.data_dir, create=True))
        self.plugins = PluginRegistry(health_store=self.store)
        self.secrets = SecretManager()
        self.routes = RouteManager(self.store)
        self.tunnels = TunnelSupervisor(lambda ref: self.secrets.resolve_operation(ref))
        self.routes.tunnel_address = self.tunnels.ensure
        # The browser asks per solve, so turning blocking off applies at once.
        from .browser_solver import set_ad_blocker
        set_ad_blocker(lambda: self._adblock() if self._ui_settings()["network"].get("adblock", True) else None)
        self._sync_active_route()
        self.fallback = ProviderFallback(self.store)
        self.archive_backend = ArchiveBackend()
        initial_ui_settings = self._ui_settings()
        general = initial_ui_settings["general"]
        network = initial_ui_settings["network"]
        # The UI setting is the source of truth.  Do not resurrect an old
        # override after a restart when the user has disabled the speed limit.
        configured_rate = normalize_bandwidth_rate(
            network.get("speedLimitVal", 0) if bool(network.get("speedLimit", False)) else 0,
            unit="mebibytes_per_second",
        )
        self.resources = ResourceManager(SchedulerPolicy(
            max_active_tasks=int(general.get("maxConcurrent", _DEFAULT_MAX_CONCURRENT)),
            max_retries=int(general.get("retryCount", 3)) if general.get("retryFailed", True) else 0,
            max_segments_per_file=int(network.get("connectionsPerFile", 8)),
            request_timeout_seconds=int(network.get("timeoutSeconds", 30)),
            bandwidth_bytes_per_second=max(0, configured_rate),
        ))
        self.plugins.set_resource_manager(self.resources)
        for provider_id, manifest in self.plugins.manifests.items():
            limits = manifest.get("limits", {}) or {}
            self.resources.configure_provider(
                provider_id,
                concurrency=limits.get("max_concurrent_items"),
                requests_per_second=limits.get("requests_per_second"),
            )
        self.rust_backend = RustTransferBackend(
            max_segments=self.resources.policy.max_segments_per_file,
            min_segment_size=self.resources.policy.min_segment_size,
            max_retries=self.resources.policy.max_retries,
        )
        self.custom_backend = CustomAsyncBackend(self.resources)
        self.backend_selector = BackendSelector()
        self.transport_metrics = TransportMetricsSink()
        # Keep the name obvious for diagnostics/integration callers while
        # retaining a single sink as the source of transport metrics.
        self.metrics_sink = self.transport_metrics
        self._canary_operations: dict[str, dict[str, Any]] = {}
        self._initialize_canary_policy()
        self.events = EventPublisher(self.store)
        # Helper sidecars are admitted from the engine-owned catalog setting.
        # The supervisor is created lazily so an absent optional helper catalog
        # cannot prevent the ordinary download engine from starting.
        self._helper_supervisor: HelperSupervisor | None = None
        self._helper_controls: dict[str, threading.Event] = {}
        self.captcha = CaptchaManager(self.store, self.events)
        self.captcha.verifier.on_accept = self._share_verified_clearance
        self.captcha.verifier.on_reject = self._quarantine_clearance
        self.captcha.recover_after_restart()
        from .challenge_scheduler import ChallengeScheduler
        self.challenge_scheduler = ChallengeScheduler(limit=2)
        from .challenge_handoff import HandoffBroker
        self.handoffs = HandoffBroker(self.captcha)
        self.captcha.set_coordinator_callback(self._on_captcha_coordinator)
        self.captcha.set_manual_required_callback(self._on_captcha_manual_required)
        self._captcha_auto_solve_attempted: set[str] = set()
        try:
            self.captcha.twocaptcha.api_key = self.secrets.resolve("keychain://captcha-2captcha") or ""
        except Exception:
            # Keychain backends may be unavailable in headless/test installs.
            pass
        self.archive_orchestrator = ArchiveOrchestrator(self.archive_backend)
        stored_flare = str(self.store.get_setting("flaresolverr_endpoint", "http://127.0.0.1:8191"))
        self.flaresolverr = FlareSolverrClient(endpoint=stored_flare)
        self.shortlink_resolver = RecursiveShortlinkResolver(self.captcha, flaresolverr=self.flaresolverr)
        self.resolution_broker = ResolutionBroker(self.store)
        self.session_broker = SessionBroker()
        self._host_sessions: dict[str, dict[str, Any]] = {}
        try:
            persisted_sessions = self.store.get_setting("provider_host_sessions", {}) or {}
            now = time.time()
            self._host_sessions = {h: s for h, s in persisted_sessions.items() if isinstance(s, dict) and s.get("expires_at", 0) > now}
        except Exception:
            self._host_sessions = {}
        self.collection_planner = CollectionPlanner()
        self.health_monitor = ProviderHealthMonitor(self.store)
        self.universal_resolver = UniversalResolver()
        self.media_assembler = MediaAssembler()
        self._controls: dict[str, _TaskControl] = {}
        self._archive_controls: dict[str, _TaskControl] = {}
        self._archive_inflight: set[str] = set()
        self._futures: dict[str, Any] = {}
        self._archive_futures: set[asyncio.Future[Any]] = set()
        self._domain_resolve_locks: dict[str, asyncio.Lock] = {}
        self._domain_last_resolved: dict[str, float] = {}
        self.resolve_stagger = ResolveStaggerController()
        # Suspended provider operations are engine-private and intentionally
        # ephemeral: task persistence and the UI receive only the challenge.
        self._provider_continuations: dict[str, dict[str, Any]] = {}
        self._telemetry_samples: dict[str, deque[tuple[float, int]]] = {}
        self._stall_watchdog_state: dict[str, tuple[str, float, float]] = {}
        self._host_cooldown_warned: dict[str, float] = {}
        # Packages whose CAPTCHA the user has already authorised solving. Hosts
        # like DataNodes issue a Turnstile per FILE, so a multipart package
        # raises a fresh challenge for every sibling; without this, each one
        # parked in needs_user and demanded another click. One click arms the
        # package and the rest solve themselves.
        self._package_autosolve_armed: set[str] = set()
        self._last_progress_save: dict[str, float] = {}
        self._last_multipart_probe: dict[str, float] = {}
        self._multipart_probe_delays: dict[str, float] = {}
        self._package_solve_inflight: set[str] = set()
        self._package_solve_lock = threading.Lock()
        # DataNodes invalidates or withholds a sibling's step-two response once
        # another package member starts consuming payload. Each package event
        # wakes tasks waiting at preflight when a real engine transition occurs;
        # persisted task state remains the readiness source of truth.
        self._multipart_transfer_events: dict[str, asyncio.Event] = {}
        self.storage_concurrency = StorageHostConcurrencyManager(self.data_dir)
        self.custom_backend.storage_concurrency = self.storage_concurrency
        self._deleted_task_ids: set[str] = set()
        try:
            from .browser_solver import solver_daemon
            solver_daemon.set_stage_callback(self._on_solver_stage_threadsafe)
        except Exception as err:
            telemetry_bus.record(
                level="WARN",
                subsystem="engine:captcha",
                message="[SOLVER_STAGE_CALLBACK_FAILED] Solver stage updates will not reach task lifecycle",
                context={"error": str(err)[:300]},
            )
        self._loop = asyncio.new_event_loop()
        self._scheduler: FairAsyncScheduler | None = None
        self._pump_task: asyncio.Task | None = None
        self._engine_paused = bool(self.store.get_setting("engine_paused", False))
        self._clipboard_watcher = ClipboardWatcher(self._ingest_clipboard)
        if bool(self.store.get_setting("clipboard_watcher_enabled", False)):
            self._clipboard_watcher.start()
        self._loop_ready = threading.Event()
        # Before the work loop starts, so nothing can pick a task up mid-recovery.
        for task in self.store.list():
            if task.state in {"queued", "pending_probe", "resolving", "preflight", "downloading", "verifying",
                              "postprocessing", "retrying"}:
                # Nothing starts on its own after a relaunch: unfinished downloads
                # come back paused with their progress (partial file, segment
                # checkpoint, completed bytes) kept, and resume when the user says so.
                task.state, task.error = "paused", None
                task.paused_reason = task.paused_reason or "Paused when MossDL closed; press Resume to continue"
                task.recovery_reason = "application_restart"
                self.store.save_with_event(task, "TaskRecovered", {"reason": task.paused_reason}, f"recovered:{task.id}")
            elif task.state in {"paused", "failed", "canceled"}:
                # Ensure paused and terminal states are strictly preserved
                pass
        self._loop_thread = threading.Thread(target=self._loop_runner, daemon=True, name="transfer-async-loop")
        self._loop_thread.start()
        self._loop_ready.wait(timeout=2)
        self._apply_captcha_preferences(initial_ui_settings["captcha"])
        if not self.store.get_setting("history_backfilled", False):
            for task in self.store.list():
                if task.state in {"completed", "failed", "canceled"}:
                    self.store.record_history(task.to_dict())
            self.store.set_setting("history_backfilled", True)
        # Self-heal multipart package keys and link leaders
        all_init_tasks = self.store.list()
        for task in all_init_tasks:
            is_mp, canonical_key, pnum = self._task_multipart_info(task)
            if is_mp:
                changed = False
                if task.package_part_number != pnum:
                    task.package_part_number = pnum
                    changed = True
                if task.package_key != canonical_key and not os.path.isabs(canonical_key):
                    task.package_key = canonical_key
                    changed = True
                if changed:
                    self.store.save(task)
        # Normalize interrupted archive jobs, then schedule each queued job once.
        self.store.recover_archive_jobs()
        scheduled_archive_ids: set[str] = set()
        for job in self.store.list_archive_jobs_for_boot():
            if job.id in scheduled_archive_ids:
                continue
            scheduled_archive_ids.add(job.id)
            self._schedule_archive_job(job.id)
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:boot",
            message="EngineService initialization complete",
            context={
                "data_dir": str(self.data_dir),
                "plugins_count": len(self.plugins.manifests),
                "loaded_plugins": sorted(list(self.plugins.manifests.keys())),
                "max_active_tasks": self.resources.policy.max_active_tasks,
            },
        )

    def close(self) -> None:
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:teardown",
            message="EngineService teardown started",
            context={"data_dir": str(self.data_dir)},
        )
        for control in self._helper_controls.values():
            control.set()
        for future in self._futures.values():
            future.cancel()
        for future in list(self._archive_futures):
            future.cancel()
        for control in self._archive_controls.values():
            control.cancel.set()
        if self._scheduler and self._loop.is_running():
            if self._pump_task:
                self._loop.call_soon_threadsafe(self._pump_task.cancel)
            shutdown = asyncio.run_coroutine_threadsafe(self._scheduler.close(), self._loop)
            try:
                shutdown.result(timeout=2)
            except Exception:
                pass
        self.plugins.close()
        try:
            from .timer_scheduler import timer_scheduler
            timer_scheduler.close()
        except Exception as err:
            telemetry_bus.record(
                level="WARN",
                subsystem="engine:teardown",
                message="[TIMER_SCHEDULER_CLOSE_FAILED] TimerScheduler did not shut down cleanly",
                context={"error": str(err)[:300]},
            )
        self.captcha.loopback_solver.stop()
        self._clipboard_watcher.stop()
        try:
            from .http_client import close_sessions
            close_sessions()
        except Exception:
            pass
        if bool(self._ui_settings()["general"].get("clearOnExit", False)):
            for task in self.store.list():
                if task.state == "completed":
                    self.store.delete(task.id)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=2)
        if not self._loop.is_running() and not self._loop.is_closed():
            self._loop.close()
        for prof in concurrency_auditor.list_profiles():
            concurrency_auditor.save_profile(prof.host)
        self.store.close()
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:teardown",
            message="EngineService teardown completed",
            context={"data_dir": str(self.data_dir)},
        )


    def _loop_runner(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._scheduler = FairAsyncScheduler(self.resources.policy.max_active_tasks)
        self._scheduler.set_error_handler(self._on_scheduler_error)
        self._loop.run_until_complete(self._scheduler.start())
        self._pump_task = self._loop.create_task(self._pump(), name="transfer-dispatcher")
        self._loop_ready.set()
        self._loop.run_forever()

    def _ingest_clipboard(self, text: str) -> None:
        for url in extract_urls(text):
            try:
                entry = self.store.save_link(new_link(url, "clipboard", "system clipboard"))
                self.events.emit("LinkGrabberAdded", None, {"link_id": entry["id"], "host": urlsplit(url).hostname})
            except Exception:
                continue

    @staticmethod
    def _account_public(account: dict[str, Any]) -> dict[str, Any]:
        """Account resources contain references, never keychain values."""
        value = dict(account)
        for key in ("secret", "secrets", "password", "token", "cookies", "oauth", "access_token", "refresh_token"):
            value.pop(key, None)
        return value

    def _effective_bandwidth_rate(self, task_id: str | None = None, queue_id: str | None = None,
                                  provider_id: str | None = None, account_id: str | None = None) -> int:
        candidates: list[tuple[str, dict[str, Any]]] = []
        if task_id:
            task = self.store.get(task_id)
            if task:
                candidates.extend(("task", profile) for profile in self.store.list_bandwidth_profiles("task", task.id))
                queue_id = queue_id or task.queue_id
                provider_id = provider_id or task.provider
        if queue_id:
            candidates.extend(("queue", profile) for profile in self.store.list_bandwidth_profiles("queue", queue_id))
        if provider_id:
            candidates.extend(("provider", profile) for profile in self.store.list_bandwidth_profiles("provider", provider_id))
        if account_id:
            candidates.extend(("account", profile) for profile in self.store.list_bandwidth_profiles("account", account_id))
        candidates.extend(("global", profile) for profile in self.store.list_bandwidth_profiles("global", None))
        now = time.localtime()
        minutes = now.tm_hour * 60 + now.tm_min
        for _scope, profile in candidates:
            if not profile.get("enabled", True):
                continue
            windows = profile.get("windows") or []
            if windows and not any(self._window_matches(window, minutes) for window in windows):
                continue
            return normalize_bandwidth_rate(profile.get("rate_bytes_per_second", 0))
        return normalize_bandwidth_rate(self.resources.policy.bandwidth_bytes_per_second)

    def _effective_bandwidth_details(self, task_id: str | None = None, queue_id: str | None = None,
                                     provider_id: str | None = None, account_id: str | None = None) -> tuple[int, str]:
        candidates: list[tuple[str, dict[str, Any]]] = []
        if task_id:
            task = self.store.get(task_id)
            if task:
                candidates.extend(("task", profile) for profile in self.store.list_bandwidth_profiles("task", task.id))
                queue_id = queue_id or task.queue_id
                provider_id = provider_id or task.provider
        if queue_id:
            candidates.extend(("queue", profile) for profile in self.store.list_bandwidth_profiles("queue", queue_id))
        if provider_id:
            candidates.extend(("provider", profile) for profile in self.store.list_bandwidth_profiles("provider", provider_id))
        if account_id:
            candidates.extend(("account", profile) for profile in self.store.list_bandwidth_profiles("account", account_id))
        candidates.extend(("global", profile) for profile in self.store.list_bandwidth_profiles("global", None))
        now = time.localtime()
        minutes = now.tm_hour * 60 + now.tm_min
        for scope, profile in candidates:
            if not profile.get("enabled", True):
                continue
            windows = profile.get("windows") or []
            if windows and not any(self._window_matches(window, minutes) for window in windows):
                continue
            return normalize_bandwidth_rate(profile.get("rate_bytes_per_second", 0)), scope
        return normalize_bandwidth_rate(self.resources.policy.bandwidth_bytes_per_second), "global"

    @staticmethod
    def _ui_setting_defaults() -> dict[str, dict[str, Any]]:
        return {
            "general": {
                "autoStart": True, "systemTray": True, "startWithWindows": False,
                "clearOnExit": False, "confirmClear": True, "hotkeys": "", "savePath": "", "maxConcurrent": _DEFAULT_MAX_CONCURRENT, "clipboardWatcher": False,
                "retryFailed": True, "retryCount": 3, "onboardingCompleted": False,
            },
            "network": {
                "speedLimit": False, "speedLimitVal": 10, "proxy": False, "proxyAddr": "",
                "connectionsPerFile": 8, "timeoutSeconds": 30,
                # Ad blocking on uBlock Origin's lists; fullLists fetches them
                # (about 4 MB, weekly), otherwise the built-in domain list is used.
                "adblock": True, "adblockFullLists": True,
            },
            "appearance": {
                "acrylic": True, "rowSize": "medium", "showSpeeds": True, "colorAccent": "#0078D4",
                "themeName": "midnight",
                "colorMode": "Dark", "language": "English (US)", "uiScale": "Auto",
                "autoFullscreenScale": True,
            },
            "notifications": {
                "notifComplete": True, "notifFailed": True, "notifPause": False, "notifSound": True,
                "soundPreset": "Windows Notify",
            },
            "captcha": {
                "captchaAcknowledged": False, "captchaMaster": False, "captchaAutoSkip": True,
                "captchaAutoSkipSecs": 30, "captchaSound": False, "captchaAudio": True,
                "captchaHcaptcha": True, "captchaRecaptcha": True, "captchaPositional": False,
                "captcha2captcha": False, "captchaFlare": False,
                "captchaFlareEndpoint": "http://127.0.0.1:8191",
                "captchaAutoOpenManual": False, "captchaShowAutoBanner": True,
                "captchaAutoSolve": False,
            },
            "routes": {"routeType": "Direct", "activeLocation": "auto",
                       # Quota handling: rotate to another route, which kinds may be used.
                       "autoSwitchOnQuota": True, "switchIncludesProxies": True, "allowDirectFallback": True},
        }

    def _ui_settings(self) -> dict[str, dict[str, Any]]:
        defaults = self._ui_setting_defaults()
        stored = self.store.get_setting("ui_settings", {}) or {}
        for section, values in defaults.items():
            if isinstance(stored.get(section), dict):
                values.update(stored[section])
        legacy_network = stored.get("network") if isinstance(stored.get("network"), dict) else {}
        migrated = False
        for key in ("maxConcurrent", "retryFailed", "retryCount"):
            if key in legacy_network and key not in (stored.get("general") or {}):
                defaults["general"][key] = legacy_network[key]
                migrated = True
            if key in defaults["network"]:
                defaults["network"].pop(key, None)
                migrated = True
        if migrated:
            self.store.set_setting("ui_settings", {**defaults, "general": dict(defaults["general"]), "network": dict(defaults["network"])})
        defaults["general"]["savePath"] = self.store.get_setting("download_directory", defaults["general"]["savePath"])
        defaults["routes"]["activeLocation"] = self.store.get_setting("active_route_profile", defaults["routes"]["activeLocation"])
        return defaults

    def _save_ui_settings(self, changes: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str], int]:
        settings = self._ui_settings()
        changed: list[str] = []
        for section, values in changes.items():
            if section not in settings or not isinstance(values, dict):
                raise ValueError(f"unknown UI settings section: {section}")
            for key, value in values.items():
                if key not in settings[section]:
                    raise ValueError(f"unknown UI setting: {section}.{key}")
                if isinstance(settings[section][key], bool) and not isinstance(value, bool):
                    raise ValueError(f"{section}.{key} must be boolean")
                if isinstance(settings[section][key], int) and not isinstance(settings[section][key], bool) and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise ValueError(f"{section}.{key} must be numeric")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    bounds = {
                        "maxConcurrent": (1, 64), "retryCount": (0, 20),
                        "speedLimitVal": (0, 102400), "connectionsPerFile": (1, 32),
                        "timeoutSeconds": (1, 3600), "captchaAutoSkipSecs": (0, 3600),
                    }
                    minimum, maximum = bounds.get(key, (None, None))
                    if minimum is not None and not minimum <= value <= maximum:
                        raise ValueError(f"{section}.{key} must be between {minimum} and {maximum}")
                settings[section][key] = value
                changed.append(f"{section}.{key}")
        self._validate_proxy_settings(settings)
        self._canonicalize_route_settings(settings, changes)
        self.store.set_setting("ui_settings", settings)
        if "general" in changes and "savePath" in changes["general"]:
            self.store.set_setting("download_directory", changes["general"]["savePath"])
        if "routes" in changes and ("activeLocation" in changes["routes"] or "routeType" in changes["routes"]):
            self.store.set_setting("active_route_profile", settings["routes"]["activeLocation"])
            self._sync_active_route()
        revision = int(self.store.get_setting("ui_settings_revision", 0) or 0) + 1
        self.store.set_setting("ui_settings_revision", revision)
        self._apply_runtime_settings(settings, changed)
        telemetry_bus.record(
            level="INFO",
            subsystem="settings:update",
            message=f"UI settings updated ({len(changed)} keys changed, rev {revision})",
            context={"changed": changed, "revision": revision},
        )
        return settings, changed, revision

    @staticmethod
    def _sanitize_host_session_data(data: dict[str, Any]) -> dict[str, Any]:
        """Keep only what may cross tasks: clearance cookies, the user agent they
        were earned with, and the route they are bound to (challenge_artifacts).
        A positive allowlist: tokens, form cookies and URLs never get in."""
        from .challenge_artifacts import CLEARANCE_COOKIES
        if not isinstance(data, dict):
            return {}
        clean = {k: data[k] for k in ("cf_clearance", "user_agent", "route_profile_id", "updated_at", "expires_at")
                 if data.get(k) is not None}
        cookies = {ck: cv for ck, cv in (data.get("cookies") or {}).items() if ck in CLEARANCE_COOKIES and cv}
        if cookies:
            clean["cookies"] = cookies
        return clean

    def set_host_session(self, host: str, session_data: dict[str, Any], ttl: float = 7200.0) -> None:
        """Stores clearance tokens/cookies for a host so sibling tasks share the warm session."""
        clean_host = host.lower().strip()
        if not clean_host:
            return
        sanitized = self._sanitize_host_session_data(session_data)
        now = time.time()
        record = {
            **sanitized,
            "updated_at": now,
            "expires_at": now + ttl,
        }
        self._host_sessions[clean_host] = record
        try:
            active = {h: self._sanitize_host_session_data(s) for h, s in self._host_sessions.items() if isinstance(s, dict) and s.get("expires_at", 0) > now}
            self.store.set_setting("provider_host_sessions", active)
        except Exception:
            pass

    def get_host_session(self, host: str, route_profile_id: str | None = None) -> dict[str, Any]:
        """Valid clearance for a host, earned on the same route; otherwise empty.

        Cloudflare binds clearance to the address that earned it, so clearance
        from one route is useless (and a fingerprint) on another."""
        clean_host = host.lower().strip()
        if not clean_host:
            return {}
        now = time.time()
        session = self._host_sessions.get(clean_host)
        if not session:
            try:
                persisted = self.store.get_setting("provider_host_sessions", {}) or {}
                if clean_host in persisted and isinstance(persisted[clean_host], dict) and persisted[clean_host].get("expires_at", 0) > now:
                    clean_entry = self._sanitize_host_session_data(persisted[clean_host])
                    clean_entry["updated_at"] = persisted[clean_host].get("updated_at", now)
                    clean_entry["expires_at"] = persisted[clean_host].get("expires_at", now + 7200.0)
                    self._host_sessions[clean_host] = clean_entry
                    session = clean_entry
            except Exception:
                pass
        if session and session.get("expires_at", 0) > now:
            earned_on = session.get("route_profile_id")
            if earned_on and (route_profile_id or "direct") != earned_on:
                telemetry_bus.record(level="INFO", subsystem="engine:captcha",
                                     message=f"[CLEARANCE_NOT_REUSED] {clean_host}: earned on {earned_on}, requested on {route_profile_id or 'direct'}",
                                     context={"host": clean_host, "reason": "route_mismatch"}, tier="engine")
                return {}
            return self._sanitize_host_session_data(session)
        return {}

    def clear_host_session(self, host: str) -> None:
        """Removes clearance session for a host when expired or explicitly challenged."""
        clean_host = host.lower().strip()
        if clean_host in self._host_sessions:
            del self._host_sessions[clean_host]
            try:
                now = time.time()
                active = {h: s for h, s in self._host_sessions.items() if isinstance(s, dict) and s.get("expires_at", 0) > now}
                self.store.set_setting("provider_host_sessions", active)
            except Exception:
                pass


    @staticmethod
    def _validate_proxy_settings(settings: dict[str, dict[str, Any]]) -> None:
        network = settings.get("network", {})
        if not network.get("proxy"):
            return
        value = str(network.get("proxyAddr", "")).strip()
        if "://" not in value:
            value = f"http://{value}"
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            raise ValueError("proxyAddr must be a host:port or http(s)://host:port address")

    def _canonicalize_route_settings(self, settings: dict[str, dict[str, Any]], changes: dict[str, Any]) -> None:
        routes = settings.get("routes", {})
        route_profiles = self.routes.profiles()
        labels = {"Direct": "direct", "SOCKS5": "socks5", "HTTP Proxy": "http_proxy",
                  "System VPN": "system_vpn", "WireGuard": "wireguard"}
        if "routes" in changes and "routeType" in changes["routes"]:
            kind = labels.get(str(routes.get("routeType")))
            profile = next((item for item in route_profiles if item.get("kind") == kind and item.get("enabled", True)), None)
            if profile is None:
                raise ValueError(f"no enabled {routes.get('routeType')} route profile is configured")
            routes["activeLocation"] = profile["id"]
        if "routes" in changes and "activeLocation" in changes["routes"]:
            profile = next((item for item in route_profiles if item.get("id") == routes.get("activeLocation")), None)
            if profile is None:
                raise ValueError("active route profile is not configured")
            routes["routeType"] = next((label for label, kind in labels.items() if kind == profile.get("kind")), "Direct")

    def _apply_runtime_settings(self, settings: dict[str, dict[str, Any]], changed: list[str]) -> None:
        general, network = settings["general"], settings["network"]
        self.resources.policy.max_active_tasks = int(general.get("maxConcurrent", _DEFAULT_MAX_CONCURRENT))
        self.resources.policy.max_retries = int(general.get("retryCount", 3)) if general.get("retryFailed", True) else 0
        self.resources.policy.max_segments_per_file = int(network.get("connectionsPerFile", 8))
        self.resources.policy.request_timeout_seconds = int(network.get("timeoutSeconds", 30))
        self.rust_backend.max_segments = self.resources.policy.max_segments_per_file
        self.rust_backend.max_retries = self.resources.policy.max_retries
        if self._scheduler and "general.maxConcurrent" in changed:
            asyncio.run_coroutine_threadsafe(
                self._scheduler.set_workers(self.resources.policy.max_active_tasks), self._loop
            ).result(timeout=5)
        if any(key in changed for key in ("network.speedLimit", "network.speedLimitVal")):
            enabled = bool(network.get("speedLimit", False))
            rate = normalize_bandwidth_rate(network.get("speedLimitVal", 0) if enabled else 0,
                                            unit="mebibytes_per_second")
            asyncio.run_coroutine_threadsafe(self.resources.set_bandwidth_rate(rate), self._loop).result(timeout=5)
            self.rust_backend.bandwidth_rate = rate
            self.store.set_setting("bandwidth_rate_override", rate)
        if any(key.startswith("network.adblock") for key in changed):
            self._refresh_filter_lists_if_due()
        if any(key.startswith("network.proxy") for key in changed):
            self._sync_proxy_route(network)
        if any(key.startswith("captcha.") for key in changed):
            self._apply_captcha_preferences(settings["captcha"])

    def _sync_proxy_route(self, network: dict[str, Any]) -> None:
        enabled = bool(network.get("proxy", False))
        if enabled:
            endpoint = str(network.get("proxyAddr", "")).strip()
            if "://" not in endpoint:
                endpoint = f"http://{endpoint}"
            self.routes.save({"id": "settings-proxy", "kind": "http_proxy", "endpoint": endpoint, "enabled": True})
            self.store.set_setting("active_route_profile", "settings-proxy")
        elif self.store.get_setting("active_route_profile") == "settings-proxy":
            self.store.set_setting("active_route_profile", "direct")
        self._sync_active_route()

    def _usable_route_profile(self, profile_id: str | None) -> dict[str, Any]:
        """A route profile ready to use: credentials resolved for this call only,
        and a WireGuard route's tunnel started in the core."""
        route_profile = next((profile for profile in self.routes.profiles()
                              if profile["id"] == (profile_id or "direct")), None)
        if route_profile is None:
            raise ValueError(f"route profile {profile_id!r} is no longer configured")
        if route_profile.get("credential_ref") and route_profile.get("kind") in {"http_proxy", "socks5", "docker_socks5"}:
            from .routes import with_proxy_credentials
            route_profile = {**route_profile, "endpoint": with_proxy_credentials(
                str(route_profile.get("endpoint") or ""), self.secrets.resolve_operation(route_profile["credential_ref"]))}
        if TunnelSupervisor.uses_core(route_profile):
            route_profile = {**route_profile, "local_proxy": self.tunnels.ensure(route_profile)}
        return route_profile

    def _sync_active_route(self) -> None:
        """Point crawls, favicons and resolvers at the active route, or block them."""
        profile_id = str(self.store.get_setting("active_route_profile", "direct") or "direct")
        raw = next((p for p in self.routes.profiles() if p["id"] == profile_id), None)
        self._retain_tunnels(profile_id)
        if raw is not None and TunnelSupervisor.uses_core(raw):
            # The first handshake takes a moment; until it completes, requests
            # wait on "connecting" rather than going out direct.
            route_http.set_active_route(profile_id, None, error="connecting the WireGuard tunnel")
            threading.Thread(target=self._connect_active_tunnel, args=(profile_id,),
                             daemon=True, name="tunnel-connect").start()
            return
        try:
            route_http.set_active_route(profile_id, route_http.proxy_url_for(self._usable_route_profile(profile_id)))
        except Exception as exc:  # an unusable route blocks requests; it never falls back to direct
            route_http.set_active_route(profile_id, None, error=str(exc))

    def _resume_after_answer(self, task: DownloadTask, challenge: Any, solution: dict[str, Any]) -> None:
        """Resume the one task an answer belongs to, carrying it to the site.

        Nothing is shared yet: siblings get clearance only once the site accepts
        the answer (_share_verified_clearance), and never its token or URLs."""
        leader = self.store.get(task.id) or task
        leader.user_challenge = {**(leader.user_challenge or {}), "challenge_id": challenge.id if challenge else None,
                                 "verifying": True, "solver_active": False,
                                 "generation": challenge_lifecycle.generation_of(challenge) if challenge else None}
        self.store.save(leader)
        if leader.state not in _ACTIVATABLE_TASK_STATES:
            # The solver landed while a resolve is still running; that resolve carries on.
            self._log_task(leader, "debug", "captcha", f"[SOLVE_RESUME_SKIPPED] answer arrived in state '{leader.state}'",
                           {"state": leader.state})
            return
        token_val = solution_token(solution) or str(solution.get("text") or "")
        secrets = {"captcha_solution": solution, "turnstile_token": token_val, "cf-turnstile-response": token_val}
        for key in ("cf_clearance", "cookies", "user_agent", "direct_url"):
            if solution.get(key):
                secrets[key] = solution[key]
        self.dispatch("resume_task", {"id": leader.id, "secrets": secrets})

    def _settle_task_challenge(self, task: DownloadTask, accepted: bool, evidence: str) -> None:
        """The resumed task's outcome is the site's verdict on the answer it carried."""
        pending = task.user_challenge if isinstance(task.user_challenge, dict) else {}
        if not pending.get("verifying") or not pending.get("challenge_id"):
            return
        verifier = self.captcha.verifier
        (verifier.accept if accepted else verifier.reject)(str(pending["challenge_id"]), evidence)
        task.user_challenge = {**pending, "verifying": False, "verified": accepted}
        self.store.save(task)

    def _share_verified_clearance(self, challenge: Any) -> None:
        """An accepted answer's reusable clearance goes to siblings on the same host
        and route only; tokens, URLs and form state stay with the task that earned them."""
        from .challenge_artifacts import shareable_session
        task = self.store.get(challenge.task_id) if challenge.task_id else None
        if task is None:
            return
        host = (urlsplit(task.source_url).hostname or (challenge.params or {}).get("host") or "").lower()
        session = {k: v for k, v in shareable_session(challenge.solution).items() if v}
        if not host or not session.get("cookies"):
            return
        route_id = task.route_profile_id or "direct"
        self.set_host_session(host, {**session, "route_profile_id": route_id})
        for sibling in self.store.list():
            if sibling.id == task.id or sibling.state != "needs_user" or sibling.user_action not in {"turnstile", "captcha"}:
                continue
            if (urlsplit(sibling.source_url).hostname or "").lower() != host or (sibling.route_profile_id or "direct") != route_id:
                continue
            # Queued again, it picks the clearance up from the host session when dispatched.
            sibling.user_action = None
            self.store.save(sibling)
            self._transition(sibling, "queued", "TaskQueued", "Resumed with clearance another download earned")

    def _quarantine_clearance(self, challenge: Any) -> None:
        """A rejected answer's clearance is not trusted again, here or in the HTTP cache."""
        from .http_client import clearance_cache
        task = self.store.get(challenge.task_id) if challenge.task_id else None
        host = (urlsplit(task.source_url).hostname if task else "") or (challenge.params or {}).get("host") or ""
        if host:
            self.clear_host_session(host.lower())
            clearance_cache.quarantine(host.lower())

    def _mullvad_account(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Mullvad by account number: one location per city, one shared device key."""
        from . import mullvad
        if params.get("provider", mullvad.PROVIDER_ID) != mullvad.PROVIDER_ID:
            raise ValueError("only Mullvad supports signing in with an account number")
        existing = [p for p in self.routes.profiles() if p["id"].startswith(f"wg-{mullvad.PROVIDER_ID}-")
                    and p.get("peer_public_key") and p.get("credential_ref")]
        ref = existing[0]["credential_ref"] if existing else None
        if method == "provider_account_logout":
            if ref:
                device_text = self.secrets.resolve_operation(ref)
                if device_text:
                    mullvad.unregister(json.loads(device_text))
            for profile in existing:
                self.tunnels.stop(profile["id"])
                self.routes.delete(profile["id"])
            if ref:
                self.secrets.delete(ref)
            self._sync_active_route()
            return {"removed": len(existing)}
        if method == "provider_account_login":
            device = mullvad.register(str(params.get("account_number") or ""))
            ref = self.secrets.put(json.dumps(device), kind="wireguard_device")
        elif not ref:
            raise ValueError("not signed in to Mullvad")
        fresh = mullvad.city_routes(mullvad.relay_list(), ref)
        for route in fresh:
            self.routes.save(route)
        # A city Mullvad no longer serves is removed; running tunnels use the
        # new server on their next start.
        fresh_ids = {route["id"] for route in fresh}
        for profile in existing:
            if profile["id"] not in fresh_ids:
                self.routes.delete(profile["id"])
            self.tunnels.stop(profile["id"])
        self._sync_active_route()
        device = json.loads(self.secrets.resolve_operation(ref) or "{}")
        return {"locations": len(fresh), "device_name": device.get("device_name")}

    def _connect_active_tunnel(self, profile_id: str) -> None:
        try:
            proxy, error = route_http.proxy_url_for(self._usable_route_profile(profile_id)), None
        except Exception as exc:
            proxy, error = None, str(exc)
        # The user may have picked another route while this one was connecting.
        if str(self.store.get_setting("active_route_profile", "direct") or "direct") == profile_id:
            route_http.set_active_route(profile_id, proxy, error=error)

    def _retain_tunnels(self, active_id: str) -> None:
        """Stop tunnels that neither the active route nor a running task uses."""
        keep = {active_id}
        for task_id in list(getattr(self, "_controls", {})):  # none yet during startup
            task = self.store.get(task_id)
            if task is not None and task.route_profile_id:
                keep.add(task.route_profile_id)
        self.tunnels.retain(keep)

    def _apply_captcha_preferences(self, captcha: dict[str, Any]) -> None:
        self.store.set_setting("captcha_ui_preferences", dict(captcha))
        config = {"solvers": [
            {"id": "audio_speech", "enabled": bool(captcha.get("captchaAudio", True))},
            {"id": "flaresolverr", "enabled": bool(captcha.get("captchaFlare", False))},
            {"id": "twocaptcha", "enabled": bool(captcha.get("captcha2captcha", False))},
        ], "sound_enabled": bool(captcha.get("captchaSound", False)),
            "auto_skip_timeout": bool(captcha.get("captchaAutoSkip", True)),
            "auto_open_browser": bool(captcha.get("captchaAutoOpenManual", True))}
        self.captcha.set_config(config)

    def _ui_snapshot(self) -> dict[str, Any]:
        health = {"ok": True, "engine_paused": self._engine_paused,
                  "active_tasks": sum(1 for future in self._futures.values() if not future.done()),
                  "plugins": self.plugins.list_health(),
                  "provider_health": self.health_monitor.snapshots()}
        return {
            "revision": int(self.store.get_setting("ui_settings_revision", 0) or 0),
            "tasks": [task.to_dict() for task in self.store.list()],
            "linkgrabber": self.store.list_links(),
            "queues": self.store.list_queues(),
            "accounts": [self._account_public(item) for item in self.store.list_accounts()],
            "routes": self.routes.profiles(),
            "providers": self.plugins.provider_catalog(),
            "captcha_pending": self.captcha.list_pending_challenges(),
            "bandwidth": self.store.list_bandwidth_profiles(),
            "notifications": [{**item, "endpoint": _redact_diagnostic(item.get("endpoint"))}
                              for item in self.store.list_notification_sinks()],
            "capabilities": self._backend_capability_snapshot(),
            "transfer_canary": self._canary_status(),
            "health": health,
            "settings": self._ui_settings(),
        }

    def _transfer_backends(self) -> dict[str, Any]:
        return {
            "custom": self.custom_backend,
            "rust": self.rust_backend,
        }

    def _backend_capability_snapshot(self) -> dict[str, dict[str, Any]]:
        return capability_snapshot(self._transfer_backends())

    @staticmethod
    def _default_canary_policy() -> dict[str, Any]:
        return {
            "enabled": False,
            "scope": "operation",
            "fallback_backend": "custom",
            "failure_classes": sorted(_CANARY_DEFAULT_FAILURE_CLASSES),
            "disabled_until": None,
            "reason": None,
        }

    def _normalize_canary_policy(self, raw: Any = None) -> dict[str, Any]:
        policy = self._default_canary_policy()
        if isinstance(raw, dict):
            policy.update({key: raw[key] for key in policy if key in raw})
        policy["enabled"] = bool(policy["enabled"])
        policy["scope"] = str(policy["scope"]).strip().lower()
        if policy["scope"] not in _CANARY_SCOPES:
            raise ValueError("canary scope must be operation, session, or global")
        policy["fallback_backend"] = str(policy["fallback_backend"]).strip().lower()
        if policy["fallback_backend"] not in _CANARY_BACKENDS:
            raise ValueError("canary fallback_backend must be custom or rust")
        classes = policy.get("failure_classes", [])
        if isinstance(classes, str):
            classes = [classes]
        if not isinstance(classes, (list, tuple, set)):
            raise ValueError("canary failure_classes must be a list")
        policy["failure_classes"] = sorted({str(value).strip().lower() for value in classes if str(value).strip()})
        if not policy["failure_classes"]:
            policy["failure_classes"] = sorted(_CANARY_DEFAULT_FAILURE_CLASSES)
        disabled_until = policy.get("disabled_until")
        if disabled_until is None or disabled_until == "":
            policy["disabled_until"] = None
        else:
            try:
                policy["disabled_until"] = float(disabled_until)
            except (TypeError, ValueError):
                raise ValueError("canary disabled_until must be a timestamp or null") from None
        policy["reason"] = _redact_diagnostic(policy.get("reason")) if policy.get("reason") else None
        return policy

    def _initialize_canary_policy(self) -> None:
        """Create the durable contract and clear session-only runtime state."""
        raw = self.store.get_setting(_CANARY_SETTING)
        policy = self._normalize_canary_policy(raw)
        if raw is None or policy != raw:
            self.store.set_setting(_CANARY_SETTING, policy)
        if policy["scope"] == "session" and policy["disabled_until"] is not None:
            policy["disabled_until"] = None
            policy["reason"] = None
            self.store.set_setting(_CANARY_SETTING, policy)

    def _canary_policy(self) -> dict[str, Any]:
        policy = self._normalize_canary_policy(self.store.get_setting(_CANARY_SETTING))
        if policy["disabled_until"] is not None and policy["disabled_until"] <= time.time():
            policy["disabled_until"] = None
            policy["reason"] = None
            self.store.set_setting(_CANARY_SETTING, policy)
        return policy

    def _canary_status(self) -> dict[str, Any]:
        policy = self._canary_policy()
        operations = {
            task_id: {key: value for key, value in state.items() if key != "task_id"}
            for task_id, state in self._canary_operations.items()
        }
        return {**policy, "active_operations": operations}

    def _update_canary_policy(self, changes: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise ValueError("canary policy must be an object")
        current = self._canary_policy()
        requested_scope = str(changes.get("scope", current["scope"])).strip().lower()
        if requested_scope not in {"operation", "session", "global"}:
            raise ValueError("canary scope must be operation, session, or global")
        if requested_scope != current["scope"] and requested_scope != "operation" \
                and not bool(changes.get("authorize_broader_scope", False)):
            raise ValueError("session/global canary scope requires authorize_broader_scope=true")
        merged = {**current, **{key: value for key, value in changes.items()
                                if key in {"enabled", "scope", "fallback_backend", "failure_classes",
                                           "disabled_until", "reason"}}}
        policy = self._normalize_canary_policy(merged)
        if not policy["enabled"]:
            policy["disabled_until"] = None
            policy["reason"] = None
        self.store.set_setting(_CANARY_SETTING, policy)
        return self._canary_status()

    def _reset_canary_policy(self) -> dict[str, Any]:
        policy = self._canary_policy()
        policy["disabled_until"] = None
        policy["reason"] = None
        self.store.set_setting(_CANARY_SETTING, policy)
        self._canary_operations.clear()
        return self._canary_status()

    def _canary_backend_override(self, task_id: str, requested_backend: str | None) -> tuple[str | None, str | None]:
        if requested_backend is not None:
            return requested_backend, None
        policy = self._canary_policy()
        disabled = self._canary_operations.get(task_id) if policy["scope"] == "operation" else None
        if disabled is None and policy["scope"] in {"session", "global"} and policy["disabled_until"]:
            disabled = policy
        if not policy["enabled"] or disabled is None:
            return requested_backend, None
        return policy["fallback_backend"], f"canary disabled: {disabled.get('reason') or 'classified failure'}"

    def _record_canary_failure(self, task_id: str, backend: str, failure_class: Any,
                               error: str) -> dict[str, Any] | None:
        policy = self._canary_policy()
        if not policy["enabled"] or backend == policy["fallback_backend"]:
            return None
        value = getattr(failure_class, "value", str(failure_class)).lower()
        if value not in set(policy["failure_classes"]):
            return None
        state = {
            "task_id": task_id,
            "backend": backend,
            "fallback_backend": policy["fallback_backend"],
            "failure_class": value,
            "reason": _redact_diagnostic(str(error)),
            "disabled_until": time.time() + 300.0,
        }
        if policy["scope"] == "operation":
            self._canary_operations[task_id] = state
        else:
            policy["disabled_until"] = state["disabled_until"]
            policy["reason"] = state["reason"]
            self.store.set_setting(_CANARY_SETTING, policy)
        self.events.emit("BackendCanaryDisabled", task_id, {
            "backend": backend,
            "fallback_backend": policy["fallback_backend"],
            "scope": policy["scope"],
            "failure_class": value,
            "reason": state["reason"],
            "disabled_until": state["disabled_until"],
        })
        return state

    def _clear_canary_operation(self, task_id: str) -> None:
        self._canary_operations.pop(task_id, None)

    def _check_task_revision(self, task_id: str, expected_revision: Any = None):
        task = self.store.get(task_id)
        if not task:
            raise KeyError("task not found")
        if expected_revision is not None and int(task.revision) != int(expected_revision):
            raise RevisionConflict(f"task revision conflict: expected {expected_revision}, got {task.revision}")
        return task

    @staticmethod
    def _assert_task_not_terminal(task: DownloadTask) -> None:
        if task.state in _TERMINAL_TASK_STATES:
            raise ValueError(f"terminal download state {task.state} cannot be changed; retry a failed download or add it again")

    @classmethod
    def _assert_task_can_activate(cls, task: DownloadTask) -> None:
        if task.state in _TERMINAL_TASK_STATES:
            raise ValueError(f"{task.state} downloads cannot be started or resumed; retry a failed download or add it again")
        if task.state not in _ACTIVATABLE_TASK_STATES:
            raise ValueError(f"downloads in state {task.state} cannot be started or resumed")

    @staticmethod
    def _window_matches(window: dict[str, Any], minutes: int) -> bool:
        try:
            start, end = int(window["start_minute"]), int(window["end_minute"])
        except (KeyError, TypeError, ValueError):
            return False
        return start <= minutes < end if start <= end else minutes >= start or minutes < end

    @staticmethod
    def _capture_origin(params: dict[str, Any]) -> tuple[str, str]:
        origin = params.get("origin") if isinstance(params.get("origin"), dict) else {}
        extension_origin = str(origin.get("extension_origin") or params.get("extension_origin") or "").strip()
        page_origin = str(origin.get("page_origin") or params.get("page_origin") or "").strip()
        return extension_origin, page_origin

    def _validate_capture_admission(self, params: dict[str, Any]) -> tuple[str, str, str | None]:
        extension_origin, page_origin = self._capture_origin(params)
        extension = urlsplit(extension_origin)
        page = urlsplit(page_origin)
        if extension.scheme not in _CAPTURE_EXTENSION_SCHEMES or not extension.netloc or extension.path not in {"", "/"}:
            raise ValueError("capture extension origin is not admitted")
        if page.scheme not in _CAPTURE_PAGE_SCHEMES or not page.hostname or page.path not in {"", "/"}:
            raise ValueError("capture page origin is not admitted")
        page_value = params.get("page") if isinstance(params.get("page"), dict) else {}
        page_url = str(page_value.get("url") or params.get("page_url") or "")
        if not page_url:
            raise ValueError("capture page URL is required")
        parsed_page_url = urlsplit(page_url)
        if parsed_page_url.scheme not in _CAPTURE_PAGE_SCHEMES or (parsed_page_url.hostname or "").lower() != (page.hostname or "").lower():
            raise ValueError("capture page URL does not match its admitted origin")
        session_ref = str(params.get("session_ref") or "").strip()
        if not session_ref:
            raise ValueError("capture session reference is required")
        session = self.session_broker.status(session_ref)
        if not session or session.get("state") != "active":
            raise ValueError("capture session reference is expired or unknown")
        candidates = params.get("candidates")
        if not isinstance(candidates, list) or not candidates or len(candidates) > _CAPTURE_MAX_CANDIDATES:
            raise ValueError("capture candidate batch is invalid or too large")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError("capture candidates must be objects")
            headers = candidate.get("headers", {})
            if not isinstance(headers, dict) or len(headers) > _CAPTURE_MAX_HEADER_COUNT:
                raise ValueError("capture headers are invalid or too large")
            if sum(len(str(key).encode("utf-8")) + len(str(value).encode("utf-8")) for key, value in headers.items()) > _CAPTURE_MAX_HEADER_BYTES:
                raise ValueError("capture headers exceed the size limit")
        return extension_origin, page_origin, session_ref

    def _capture_batch(self, params: dict[str, Any]) -> dict[str, Any]:
        from .browser_bridge import PROTOCOL_VERSION

        if params.get("version") != PROTOCOL_VERSION or params.get("type") != "candidate_batch":
            raise ValueError("unsupported browser capture protocol version")
        batch_id, request_id = str(params.get("batch_id") or "").strip(), str(params.get("request_id") or "").strip()
        if not batch_id or not request_id:
            raise ValueError("capture batch_id and request_id are required")
        existing = self.store.get_capture_batch(batch_id=batch_id) or self.store.get_capture_batch(request_id=request_id)
        if existing:
            if existing["batch_id"] != batch_id or existing["request_id"] != request_id:
                raise ValueError("capture batch identity was reused with different request data")
            extension_origin, page_origin = self._capture_origin(params)
            if extension_origin != existing["extension_origin"] or page_origin != existing["page_origin"]:
                raise ValueError("capture batch origin does not match the admitted batch")
            if str(params.get("session_ref") or "") != str(existing.get("session_ref") or ""):
                raise ValueError("capture batch session does not match the admitted batch")
            return {"batch": existing, "replayed": True}
        extension_origin, page_origin, session_ref = self._validate_capture_admission(params)
        page = params.get("page") if isinstance(params.get("page"), dict) else {}
        page_url = str(page.get("url") or params.get("page_url") or "") or None
        values = []
        for raw in list(params.get("candidates") or []):
            if not isinstance(raw, dict):
                raise ValueError("capture candidates must be objects")
            values.append({**raw, "page_url": page_url, "page_origin": page_origin,
                           "session_ref": session_ref, "request_id": raw.get("request_id") or request_id})
        ranked = rank_candidates(values, page_url=page_url, limit=min(128, len(values) or 1))
        if not ranked:
            raise ValueError("browser capture did not contain a usable candidate")
        candidates = []
        for candidate in ranked:
            public = public_candidate(candidate)
            public["candidate_id"] = hashlib.sha256(
                f"{batch_id}:{public.get('source_fingerprint') or public.get('url')}".encode("utf-8")
            ).hexdigest()[:24]
            candidates.append(public)
        batch = CaptureBatch(batch_id=batch_id, request_id=request_id, protocol_version=PROTOCOL_VERSION,
                             extension_origin=extension_origin, page_origin=page_origin, page_url=page_url,
                             session_ref=session_ref,
                             candidates=candidates, acknowledged=True)
        saved = self.store.save_capture_batch(batch)
        self.events.emit("CaptureBatchReceived", None, {"batch_id": batch_id, "request_id": request_id,
                                                         "candidate_count": len(candidates), "state": saved["state"]},
                         f"capture-batch:{batch_id}")
        return {"batch": saved, "replayed": False}

    def _capture_import(self, params: dict[str, Any]) -> dict[str, Any]:
        batch_id = str(params.get("batch_id") or "")
        batch = self.store.get_capture_batch(batch_id=batch_id)
        if not batch:
            raise KeyError("capture batch not found")
        candidates = batch.get("candidates") or []
        candidate_id = params.get("candidate_id")
        index = params.get("candidate_index", 0)
        if candidate_id is not None:
            selected = next((item for item in candidates if item.get("candidate_id") == candidate_id), None)
        else:
            try:
                selected = candidates[int(index)]
            except (IndexError, TypeError, ValueError):
                selected = None
        if not selected:
            raise KeyError("capture candidate not found")
        fingerprint = str(selected.get("source_fingerprint") or source_fingerprint(str(selected.get("url") or "")))
        existing = self.store.find_nonterminal_task_by_fingerprint(fingerprint)
        add_anyway = bool(params.get("add_anyway")) or str(params.get("duplicate_strategy", "skip")) == "add_anyway"
        if existing and not add_anyway:
            self.store.update_capture_batch(batch_id, state="duplicate")
            return {"status": "duplicate", "existing_task_id": existing.id, "task": existing.to_dict(),
                    "batch_id": batch_id, "candidate_id": selected.get("candidate_id"), "add_anyway_available": True}
        task_params = {"url": selected["url"], "destination": params.get("destination"),
                       "display_name": selected.get("filename"), "priority": params.get("priority", 0),
                       "queue_id": params.get("queue_id", "default"), "category": params.get("category"),
                       "duplicate_strategy": "rename" if add_anyway else params.get("duplicate_strategy", "skip"),
                       "request_headers": selected.get("headers", {}), "referrer": selected.get("referrer"),
                       "browser_context": {"capture_source": "browser", "batch_id": batch_id,
                                           "candidate_id": selected.get("candidate_id"), "page_url": batch.get("page_url"),
                                           "page_origin": batch.get("page_origin"), "session_ref": batch.get("session_ref")}}
        task = self.dispatch("add_task", task_params)
        imported = list(batch.get("imported_task_ids") or []) + [task["id"]]
        self.store.update_capture_batch(batch_id, state="imported", imported_task_ids=imported)
        return {"status": "imported", "task": task, "batch_id": batch_id,
                "candidate_id": selected.get("candidate_id"),
                "existing_task_id": existing.id if existing else None, "add_anyway": add_anyway}

    def _helper_supervisor_instance(self) -> HelperSupervisor:
        if self._helper_supervisor is None:
            catalog = self.store.get_setting("helper_catalog", [])
            self._helper_supervisor = HelperSupervisor(
                catalog,
                secret_manager=self.secrets,
                resource_manager=self.resources,
                event_publisher=self.events,
                max_active=1,
            )
        return self._helper_supervisor

    def _helper_invoke(self, params: dict[str, Any]) -> dict[str, Any]:
        helper_id = str(params.get("helper_id") or "")
        operation = str(params.get("operation") or "")
        host = _normalize_input_url(params.get("url") or params.get("host") or "")
        operation_id = str(params.get("operation_id") or uuid4())
        job = make_job("helper", [{"operation": operation, "helper_id": helper_id}], operation_id)
        self.store.save_workflow(job)
        workflow_transition(job, "running")
        self.store.save_workflow(job)
        cancel_event = threading.Event()
        self._helper_controls[operation_id] = cancel_event
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._helper_supervisor_instance().run_async(
                    helper_id, operation, host, params.get("params") or {},
                    credential_ref=params.get("credential_ref"), cancel_event=cancel_event,
                    task_id=operation_id, version=params.get("version")),
                self._loop,
            )
            outcome = future.result(timeout=float(params.get("timeout_seconds", 45)) + 5)
            if outcome.status == "completed":
                workflow_transition(job, "completed")
            elif outcome.status == "cancelled":
                workflow_transition(job, "canceled", error=outcome.error)
            else:
                workflow_transition(job, "failed", error=outcome.error or outcome.status)
            job.progress = 1.0
            self.store.save_workflow(job)
            return {"operation_id": operation_id, "job": job.to_dict(), "outcome": outcome.to_dict()}
        except Exception as exc:
            if job.state == "running":
                workflow_transition(job, "failed", error=_redact_diagnostic(str(exc)))
                self.store.save_workflow(job)
            raise
        finally:
            self._helper_controls.pop(operation_id, None)

    def _helper_status(self, params: dict[str, Any]) -> dict[str, Any]:
        operation_id = params.get("operation_id") or params.get("id")
        if operation_id:
            job = self.store.get_workflow(str(operation_id))
            if not job or job.get("kind") != "helper":
                raise KeyError("helper operation not found")
            events = self.store.events_since(int(params.get("after_id", 0)), int(params.get("limit", 100)))
            events = [event for event in events if event.get("task_id") == str(operation_id)]
            return {"operation_id": str(operation_id), "job": job, "events": self._public_events(events)}
        jobs = [job for job in self.store.list_workflows() if job.get("kind") == "helper"]
        return {"operations": jobs}

    def _helper_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        operation_id = str(params.get("operation_id") or params.get("id") or "")
        if not operation_id:
            raise ValueError("operation_id is required")
        job = self.store.get_workflow(operation_id)
        if not job or job.get("kind") != "helper":
            raise KeyError("helper operation not found")
        if job["state"] in {"completed", "failed", "canceled"}:
            return job
        control = self._helper_controls.get(operation_id)
        if control is not None:
            control.set()
        if job["state"] == "queued":
            workflow = make_job("helper", job.get("steps", []), operation_id)
            workflow.state = "queued"
            workflow.revision = int(job.get("revision", 0))
            workflow.created_at = float(job.get("created_at", time.time()))
            workflow_transition(workflow, "canceled", error="canceled by client")
            job = self.store.save_workflow(workflow)
        return job

    @staticmethod
    def _public_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{
            "id": int(event["id"]), "event_id": str(event["id"]),
            "event_type": event.get("event_type"), "task_id": event.get("task_id"),
            "resource_id": event.get("task_id"), "created_at": event.get("created_at"),
            "payload": _redact_diagnostic(event.get("payload") or (
                json.loads(event.get("payload_json") or "{}") if event.get("payload_json") else {})),
        } for event in events]

    def pre_dispatch(self, request_or_method: Any, *args: Any, **kwargs: Any) -> Any:
        return pre_dispatch(self, request_or_method, *args, **kwargs)

    def dispatch(self, method: str, params: dict[str, Any] | None = None) -> Any:
        start_time = time.time()
        try:
            result = self._dispatch_internal(method, params)
            duration_ms = (time.time() - start_time) * 1000
            _POLL_METHODS = {
                "logs_query", "list_events", "list_collection_plans",
                "capture_review_list", "get_download_directory", "ui_snapshot",
                "get_settings", "list_tasks", "list_queues"
            }
            if method not in _POLL_METHODS or duration_ms > 250:
                telemetry_bus.record(
                    level="WARN" if duration_ms > 500 else "DEBUG",
                    subsystem="engine:rpc",
                    message=f"RPC {method} completed in {duration_ms:.1f}ms",
                    context={"method": method, "params": params, "slow": duration_ms > 250},
                    duration_ms=duration_ms,
                )
            return result

        except Exception as exc:
            duration_ms = (time.time() - start_time) * 1000
            import traceback
            tb = traceback.format_exc()
            telemetry_bus.record(
                level="ERROR",
                subsystem="engine:rpc",
                message=f"RPC {method} failed: {type(exc).__name__}: {exc}",
                context={"method": method, "params": params},
                error={"type": type(exc).__name__, "message": str(exc), "stack": tb},
                duration_ms=duration_ms,
            )
            raise

    def _dispatch_internal(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if method == "logs_query":
            return self._logs_query(params)
        if method == "logs_clear":
            return self._logs_clear(params)
        if method == "logs_export":
            return self._logs_export(params)
        if method == "log_event":
            return self._log_event(params)
        if method == "critical_trace_start":
            output_dir = params.get("output_dir") or (self.data_dir / "benchmarks" / "critical-traces")
            return critical_trace.start(
                output_dir,
                run_id=params.get("run_id"),
                queue_size=int(params.get("queue_size", 20_000)),
                attributes={
                    "label": params.get("label"),
                    "profile": params.get("profile"),
                    "mode": params.get("mode"),
                },
            )
        if method == "critical_trace_stop":
            result = critical_trace.stop()
            trace_path = result.get("trace_path")
            if trace_path:
                from .trace_analysis import analyze_trace
                report = analyze_trace(trace_path)
                report_path = Path(result["directory"]) / "analysis.json"
                report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
                result["analysis_path"] = str(report_path)
                result["analysis"] = report
            return result
        if method == "critical_trace_status":
            return {"active": critical_trace.active()}
        if method == "api_info":
            return api_description()

        if method == "ui_snapshot":
            return self._ui_snapshot()
        if method == "ui_settings_get":
            return self._ui_settings()
        if method == "ui_settings_update":
            settings, changed, revision = self._save_ui_settings(params.get("settings", {}))
            return {"settings": settings, "changed": changed, "revision": revision}
        if method in {"backend_canary_status", "transfer_canary_status"}:
            return self._canary_status()
        if method in {"backend_canary_update", "transfer_canary_update"}:
            return self._update_canary_policy(params.get("policy", params))
        if method in {"backend_canary_reset", "transfer_canary_reset"}:
            return self._reset_canary_policy()
        if method == "client_register":
            token = str(params.get("token") or std_secrets.token_urlsafe(32))
            client_id = str(params.get("id") or uuid4())
            scopes = [str(scope) for scope in params.get("scopes", ["read", "enqueue"])]
            unknown = sorted(set(scopes) - _API_SCOPES)
            if unknown:
                raise ValueError(f"unknown client scopes: {', '.join(unknown)}")
            client = self.store.save_api_client({"id": client_id,
                "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "label": params.get("label", client_id), "scopes": scopes})
            return {"client": client, "token": token}
        if method == "client_list":
            return self.store.list_api_clients()
        if method == "client_revoke":
            if not self.store.revoke_api_client(params["id"]):
                raise KeyError("client not found")
            return {"revoked": params["id"]}
        if method == "worker_hello":
            hello = worker_hello(params.get("worker_id", ""), params.get("capabilities", []), params.get("protocol_version", API_VERSION))
            saved = self.store.save_worker({"id": hello["worker_id"], "protocol_version": hello["protocol_version"],
                                            "capabilities": hello["capabilities"], "metadata": params.get("metadata", {})})
            return {**saved, "worker_id": hello["worker_id"]}
        if method == "worker_list":
            return self.store.list_workers()
        if method == "worker_heartbeat":
            return {"updated": self.store.heartbeat_worker(params["worker_id"], params.get("lease_id"))}
        if method == "worker_lease":
            result = self.store.lease_worker(params["worker_id"], params.get("lease_id", str(uuid4())))
            if not result:
                raise ValueError("worker is not registered or already leased")
            return result
        if method == "worker_release":
            return {"released": self.store.release_worker(params["worker_id"], params.get("lease_id"), params.get("state", "active"))}
        if method == "workflow_create":
            job = make_job(params.get("kind", "generic"), params.get("steps"), params.get("id"))
            return self.store.save_workflow(job)
        if method == "workflow_get":
            result = self.store.get_workflow(params["id"])
            if not result:
                raise KeyError("workflow not found")
            return result
        if method == "workflow_list":
            return self.store.list_workflows(params.get("state"))
        if method == "workflow_transition":
            raw = self.store.get_workflow(params["id"])
            if not raw:
                raise KeyError("workflow not found")
            from .platform import WorkflowJob
            job = WorkflowJob(**raw)
            workflow_transition(job, params["state"], error=params.get("error"))
            return self.store.save_workflow(job)
        if method == "backup_create":
            target = Path(params.get("path") or (self.data_dir / "backups" / f"transfer-{int(time.time())}.sqlite3"))
            return {"path": self.store.backup_database(target), "migration": self.store.migration_status()}
        if method == "backup_restore":
            target = Path(params["destination"]).resolve()
            return {"path": self.store.restore_database(params["source"], target), "migration": self.store.migration_status()}
        if method == "secret_put":
            value = str(params["value"])
            name = params.get("name")
            reference = self.secrets.put(value, kind=params.get("kind", "opaque"), name=name)
            if name == "captcha-2captcha":
                self.captcha.twocaptcha.api_key = value
            return {"credential_ref": reference}
        if method == "secret_rotate":
            return {"credential_ref": self.secrets.rotate(params["credential_ref"], params["value"])}
        if method == "secret_delete":
            return {"deleted": self.secrets.delete(params["credential_ref"])}
        if method == "secret_validate":
            return {"credential_ref": self.secrets.validate(params["credential_ref"])}
        if method == "check_destination_collision":
            dest_str = str(params.get("destination") or "").strip()
            filename = str(params.get("filename") or "").strip()
            items = params.get("items") or []
            if not dest_str or not filename:
                return {"exists": False, "primary_exists": False, "suggested_name": None, "existing_items": []}
            dest = Path(dest_str).resolve()
            target = (dest / filename).resolve()
            primary_exists = target.exists()
            existing_items = []
            for it in items:
                if it and isinstance(it, str):
                    cand = (dest / it).resolve()
                    if cand.exists():
                        existing_items.append(it)
            exists = primary_exists or len(existing_items) > 0
            suggested_name = None
            if exists:
                stem = target.stem
                suffix = target.suffix
                counter = 1
                while counter < 1000:
                    candidate = dest / f"{stem} ({counter}){suffix}"
                    if not candidate.exists():
                        suggested_name = f"{stem} ({counter}){suffix}"
                        break
                    counter += 1
            return {
                "exists": exists,
                "primary_exists": primary_exists,
                "path": str(target),
                "size": target.stat().st_size if primary_exists and target.is_file() else None,
                "existing_items": existing_items,
                "suggested_name": suggested_name,
            }
        if method == "list_tasks":
            return [task.to_dict() for task in self.store.list()]
        if method == "task_status":
            task = self.store.get(params["id"])
            if not task:
                raise KeyError("task not found")
            return task.to_dict()
        if method == "task_transitions":
            if not self.store.get(params["id"]):
                raise KeyError("task not found")
            return self.store.list_transitions(params["id"], int(params.get("limit", 200)))
        if method == "task_heartbeat":
            return {"updated": self.store.heartbeat_task(params["id"], params.get("lease_id"))}
        if method == "backend_capabilities":
            return self._backend_capability_snapshot()
        if method == "task_recover":
            task = self.store.get(params["id"])
            if not task:
                raise KeyError("task not found")
            self._assert_task_not_terminal(task)
            previous = task.state
            task.state, task.error, task.recovery_reason = "queued", None, params.get("reason", "manual_recovery")
            task.lease_id, task.heartbeat_at = None, None
            self.store.save_with_event(task, "TaskRecovered", {"reason": task.recovery_reason}, from_state=previous)
            return task.to_dict()
        if method == "health":
            return {"ok": True, "engine_paused": self._engine_paused,
                    "active_tasks": sum(1 for future in self._futures.values() if not future.done()),
                    "plugins": self.plugins.list_health(),
                    "provider_health": self.health_monitor.snapshots()}
        if method == "export_definitions":
            return {"version": 1, "queues": self.store.list_queues(), "tasks": [
                        {"source_url": task.source_url, "destination": task.destination, "display_name": task.display_name,
                         "password_ref": task.password_ref, "priority": task.priority, "queue_id": task.queue_id,
                         "queue_order": task.queue_order, "scheduled_at": task.scheduled_at, "category": task.category,
                         "duplicate_strategy": task.duplicate_strategy, "alternate_urls": task.alternate_urls,
                         "request_headers": task.request_headers, "referrer": task.referrer,
                         "browser_context": task.browser_context,
                         "selected_item_ids": task.selected_item_ids} for task in self.store.list()]}
        if method == "import_definitions":
            imported = []
            for queue in params.get("queues", []):
                if queue.get("id") and queue.get("id") != "default":
                    self.store.save_queue(queue)
            for definition in params.get("tasks", []):
                imported.append(self.dispatch("add_task", definition))
            imported_tasks = [self.store.get(item["id"]) for item in imported if item.get("id")]
            pkg_map: dict[str, list[DownloadTask]] = {}
            for t in imported_tasks:
                if t:
                    is_mp, pkey, pnum = self._task_multipart_info(t)
                    if is_mp and pkey:
                        pkg_map.setdefault(pkey, []).append(t)
            for pkey, ptasks in pkg_map.items():
                part1 = next((t for t in ptasks if self._task_multipart_info(t)[2] == 1), None)
                if part1 and part1.state not in {"downloading", "verifying", "postprocessing", "completed"}:
                    for other in ptasks:
                        if self._task_multipart_info(other)[2] > 1 and other.state == "queued":
                            other.state = "pending_probe"
                            self.store.save(other)
            return {"imported": imported}
        if method == "list_queues":
            return self.store.list_queues()
        if method == "save_queue":
            queue = dict(params)
            if not queue.get("id"):
                raise ValueError("queue id is required")
            self.store.save_queue(queue)
            return self.store.get_queue(queue["id"])
        if method == "delete_queue":
            self.store.delete_queue(params["id"])
            return {"deleted": params["id"]}
        if method in {"pause_queue", "resume_queue"}:
            queue = self.store.get_queue(params["id"])
            if not queue:
                raise KeyError("queue not found")
            queue["paused"] = method == "pause_queue"
            self.store.save_queue(queue)
            return queue
        if method == "pause_engine":
            self._engine_paused = True
            self.store.set_setting("engine_paused", True)
            return {"paused": True}
        if method == "resume_engine":
            self._engine_paused = False
            self.store.set_setting("engine_paused", False)
            return {"paused": False}
        if method in {"tasks_pause_all", "tasks_resume_all", "tasks_stop_all"}:
            return self._bulk_transfer_action(method, params)
        if method == "list_plugins":
            return self.plugins.manifests_with_health()
        if method == "inspect_url":
            return self.plugins.inspect_url(_normalize_input_url(params["url"]))
        if method == "intake_analyze":
            return self._intake_analyze(str(params.get("url") or ""))
        if method == "list_plugin_health":
            return self.plugins.list_health()
        if method == "set_plugin_state":
            return self.plugins.set_state(params["plugin_id"], params.get("enabled"), params.get("quarantined"))
        if method == "list_accounts":
            return [self._account_public(item) for item in self.store.list_accounts(params.get("provider_id"))]
        if method in {"account_create", "account_update"}:
            account = dict(params)
            secret_value = account.pop("secret_value", None)
            if secret_value is not None:
                account["credential_ref"] = self.secrets.put(str(secret_value), kind=account.get("account_type", "credential"))
            if not account.get("id"):
                account["id"] = str(uuid4())
            if not account.get("provider_id") or not account.get("credential_ref"):
                raise ValueError("provider_id and credential_ref are required")
            if not str(account["credential_ref"]).startswith(("keychain://", "secret://")):
                raise ValueError("credential_ref must be an opaque keychain:// or secret:// reference")
            self.store.save_account(account)
            result = next(item for item in self.store.list_accounts(account["provider_id"]) if item["id"] == account["id"])
            self.events.emit("AccountChanged", None, {"account_id": account["id"], "provider_id": account["provider_id"], "operation": method})
            return self._account_public(result)
        if method == "save_account":
            account = dict(params)
            if not account.get("id") or not account.get("provider_id") or not account.get("credential_ref"):
                raise ValueError("account id, provider_id, and credential_ref are required")
            if not str(account["credential_ref"]).startswith(("keychain://", "secret://")):
                raise ValueError("credential_ref must be an opaque keychain:// or secret:// reference")
            self.store.save_account(account)
            return self._account_public(next(item for item in self.store.list_accounts(account["provider_id"]) if item["id"] == account["id"]))
        if method == "account_delete":
            account_id = params["account_id"]
            existing = next((item for item in self.store.list_accounts() if item["id"] == account_id), None)
            if existing:
                if existing.get("credential_ref"):
                    try:
                        self.secrets.delete(existing["credential_ref"])
                    except Exception:
                        pass
                self.store.delete_account(account_id)
                self.events.emit("AccountChanged", None, {"account_id": account_id, "operation": "account_delete"})
                return {"deleted": account_id}
            return {"deleted": None}
        if method == "google_oauth_get_auth_url":
            import urllib.parse as _u_parse
            client_id = params.get("client_id") or "1072944744833-av34e3vvh4h86uh3g3289v9qf461v8ve.apps.googleusercontent.com"
            redirect_uri = params.get("redirect_uri") or "urn:ietf:wg:oauth:2.0:oob"
            scope = _u_parse.quote("https://www.googleapis.com/auth/drive.readonly")
            auth_url = (
                f"https://accounts.google.com/o/oauth2/v2/auth?"
                f"client_id={_u_parse.quote(client_id)}&"
                f"redirect_uri={_u_parse.quote(redirect_uri)}&"
                f"response_type=code&"
                f"scope={scope}&"
                f"access_type=offline&prompt=consent"
            )
            return {"auth_url": auth_url, "client_id": client_id, "scope": "drive.readonly"}
        if method == "account_health":
            account = next((item for item in self.store.list_accounts(params.get("provider_id"))
                            if item["id"] == params["account_id"]), None)
            if not account:
                raise KeyError("account not found")
            now = time.time()
            expired = account.get("expires_at") is not None and float(account["expires_at"]) <= now
            quarantined = account.get("quarantine_until") is not None and float(account["quarantine_until"]) > now
            state = "expired" if expired else ("quarantined" if quarantined else account.get("state", "unknown"))
            self.store.update_account_health(account["id"], state, account.get("last_error"), account.get("health", {}), now)
            return self._account_public(next(item for item in self.store.list_accounts(account["provider_id"]) if item["id"] == account["id"]))
        if method == "select_account":
            provider_id = params["provider_id"]
            now = time.time()
            candidates = [item for item in self.store.list_accounts(provider_id)
                          if item.get("enabled") and item.get("state") not in {"disabled", "expired", "invalid"}
                          and not (item.get("quarantine_until") is not None and float(item["quarantine_until"]) > now)
                          and (item.get("expires_at") is None or float(item["expires_at"]) > now)
                          and (item.get("quota_bytes") is None or int(item.get("used_bytes", 0)) < int(item["quota_bytes"]))]
            override = params.get("account_id")
            if override:
                candidates = [item for item in candidates if item["id"] == override]
            candidates.sort(key=lambda item: (item.get("state") != "healthy", -int(item.get("priority", 0)),
                                              item.get("used_bytes", 0), item["id"]))
            selected = candidates[0] if candidates else None
            return self._account_public(selected) if selected else None
        if method == "account_select":
            return self.dispatch("select_account", params)
        if method == "account_quota":
            account = next((item for item in self.store.list_accounts(params.get("provider_id"))
                            if item["id"] == params["account_id"]), None)
            if not account:
                raise KeyError("account not found")
            quota, used = account.get("quota_bytes"), int(account.get("used_bytes", 0))
            return {"account_id": account["id"], "quota_bytes": quota, "used_bytes": used,
                    "remaining_bytes": None if quota is None else max(0, int(quota) - used),
                    "quota_reset_at": account.get("quota_reset_at")}
        if method == "delete_account":
            self.store.delete_account(params["id"])
            return {"deleted": params["id"]}
        if method == "account_health_check":
            return self.dispatch("account_health", params)
        if method == "list_linkgrabber":
            return self.store.list_links(params.get("state"), params.get("query"))
        if method == "linkgrabber_add":
            entries = []
            for url in extract_urls(str(params.get("text", ""))) or [str(params.get("url", ""))]:
                try:
                    entries.append(self.store.save_link(new_link(url, params.get("source", "manual"), params.get("source_context"))))
                except ValueError:
                    continue
            return entries
        if method == "linkgrabber_update":
            changes = {key: params[key] for key in ("state", "selected", "title", "error") if key in params}
            result = self.store.update_link(params["id"], **changes)
            if not result:
                raise KeyError("link not found")
            return result
        if method == "linkgrabber_bulk_update":
            ids = [str(item) for item in params.get("ids", [])]
            changed = self.store.bulk_update_links(ids, **{key: params[key] for key in ("state", "selected", "title", "error") if key in params})
            return {"updated": changed, "ids": ids}
        if method == "linkgrabber_bulk_delete":
            ids = [str(item) for item in params.get("ids", [])]
            for link_id in ids:
                self.store.delete_link(link_id)
            return {"deleted": len(ids)}
        if method == "linkgrabber_retry":
            ids = set(str(item) for item in params.get("ids", []))
            entries = self.store.list_links(params.get("state"))
            if ids:
                entries = [entry for entry in entries if entry["id"] in ids]
            changed = 0
            for entry in entries:
                if entry["state"] in {"failed", "ignored"}:
                    self.store.update_link(entry["id"], state="pending", error=None)
                    changed += 1
            return {"retried": changed}
        if method in {"linkgrabber_inspect", "linkgrabber_resolve"}:
            entry = next((item for item in self.store.list_links() if item["id"] == params["id"]), None)
            if not entry:
                raise KeyError("link not found")
            url = entry["normalized_url"]
            if method == "linkgrabber_resolve":
                future = asyncio.run_coroutine_threadsafe(self.shortlink_resolver.resolve_chain(url), self._loop)
                url, _hops = future.result(timeout=60)
            result = self.universal_resolver.inspect(url, headers=params.get("headers"))
            provider = (result.get("candidates") or [{}])[0].get("provider")
            self.store.update_link(entry["id"], state="resolved" if result.get("candidates") else "failed",
                                   provider_id=provider, title=(result.get("candidates") or [{}])[0].get("filename"),
                                   error=None if result.get("candidates") else "no downloadable candidates")
            return {"link": next(item for item in self.store.list_links() if item["id"] == entry["id"]), **result}
        if method == "linkgrabber_delete":
            self.store.delete_link(params["id"])
            return {"deleted": params["id"]}
        if method == "linkgrabber_clear":
            for entry in self.store.list_links(params.get("state")):
                self.store.delete_link(entry["id"])
            return {"cleared": True}
        if method == "clipboard_get_status":
            return {"enabled": bool(self._clipboard_watcher.enabled), "supported": hasattr(__import__("ctypes"), "windll")}
        if method == "clipboard_set_enabled":
            enabled = bool(params.get("enabled", False))
            self.store.set_setting("clipboard_watcher_enabled", enabled)
            if enabled:
                self._clipboard_watcher.start()
            else:
                self._clipboard_watcher.stop()
            return {"enabled": enabled}
        if method == "clipboard_set_autostart":
            consent = bool(params.get("consent", False))
            enabled = bool(params.get("enabled", False)) and consent
            registration = set_user_autostart(enabled, command=params.get("command"), data_dir=self.data_dir) if consent else set_user_autostart(False, data_dir=self.data_dir)
            self.store.set_setting("clipboard_autostart_consent", consent)
            self.store.set_setting("clipboard_autostart_enabled", bool(registration.get("enabled")))
            return {**registration, "consent": consent}
        if method == "clipboard_get_autostart":
            return {**get_user_autostart(), "consent": bool(self.store.get_setting("clipboard_autostart_consent", False))}
        if method == "app_set_autostart":
            enabled = bool(params.get("enabled", False))
            result = set_user_autostart(enabled, command=params.get("command"), data_dir=self.data_dir,
                                        value_name="TransferManager")
            self.store.set_setting("app_autostart_enabled", bool(result.get("enabled")))
            return result
        if method == "app_get_autostart":
            return get_user_autostart("TransferManager")
        if method == "linkgrabber_enqueue":
            entries = self.store.list_links(params.get("state", "pending"))
            ids = set(str(value) for value in params.get("ids", []))
            if ids:
                entries = [entry for entry in entries if entry["id"] in ids]
            created = []
            for entry in entries:
                if entry["state"] not in {"pending", "resolved", "failed"}:
                    continue
                task_params = {
                    "url": entry["normalized_url"],
                    "destination": params.get("destination"),
                    "category": params.get("category", "linkgrabber"),
                }
                if entry.get("title") or entry.get("display_name"):
                    task_params["display_name"] = entry.get("title") or entry.get("display_name")
                if entry.get("size") is not None:
                    task_params["size"] = entry.get("size")
                if entry.get("folder_path"):
                    task_params["folder_path"] = entry.get("folder_path")
                task = self.dispatch("add_task", task_params)
                self.store.update_link(entry["id"], state="queued", selected=False, title=task.get("display_name"))
                created.append(task)
            return created
        if method == "list_bandwidth_profiles":
            return self.store.list_bandwidth_profiles(params.get("scope"), params.get("scope_key"))
        if method == "save_bandwidth_profile":
            profile = dict(params)
            if not profile.get("id"):
                raise ValueError("bandwidth profile id is required")
            result = self.store.save_bandwidth_profile(profile)
            effective, scope = self._effective_bandwidth_details()
            if self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self.resources.set_bandwidth_rate(effective), self._loop).result(timeout=5)
            self.rust_backend.bandwidth_rate = effective
            self.events.emit("BandwidthPolicyChanged", None, {"profile_id": profile["id"],
                                                               "effective_rate": effective, "scope": scope,
                                                               "unit": "bytes_per_second"})
            return result
        if method == "delete_bandwidth_profile":
            self.store.delete_bandwidth_profile(params["id"])
            return {"deleted": params["id"]}
        if method == "set_bandwidth_rate":
            rate = normalize_bandwidth_rate(params.get("rate_bytes_per_second", 0))
            future = asyncio.run_coroutine_threadsafe(self.resources.set_bandwidth_rate(rate), self._loop)
            future.result(timeout=5)
            self.rust_backend.bandwidth_rate = rate
            self.store.set_setting("bandwidth_rate_override", rate)
            return {"rate_bytes_per_second": rate}
        if method == "get_effective_bandwidth":
            rate, scope = self._effective_bandwidth_details(params.get("task_id"), params.get("queue_id"),
                                                             params.get("provider_id"), params.get("account_id"))
            return {"rate_bytes_per_second": rate, "scope": scope, "unit": "bytes_per_second"}
        if method == "bandwidth_admission":
            rate, scope = self._effective_bandwidth_details(params.get("task_id"), params.get("queue_id"),
                                                             params.get("provider_id"), params.get("account_id"))
            return {"rate_bytes_per_second": rate, "scope": scope, "unit": "bytes_per_second",
                    "paused": self._engine_paused, "precedence": ["task", "queue", "provider", "account", "global", "unlimited"]}
        if method == "adaptive_transfer_status":
            return {"global_in_flight": self.resources.adaptive._global_in_flight,
                    "controllers": self.resources.adaptive.snapshot()}
        if method == "authenticate_account":
            account = next((item for item in self.store.list_accounts(params.get("provider_id"))
                            if item["id"] == params["account_id"]), None)
            if not account:
                raise KeyError("account not found")
            result = self.plugins.authenticate(account["provider_id"], {"account": account, "secrets": params.get("secrets", {})})
            account["state"], account["last_error"] = "healthy", None
            self.store.save_account(account)
            return result
        if method == "refresh_account":
            account = next((item for item in self.store.list_accounts(params.get("provider_id"))
                            if item["id"] == params["account_id"]), None)
            if not account:
                raise KeyError("account not found")
            self.store.update_account_refresh(account["id"], "refreshing", error=None)
            try:
                result = self.plugins.authenticate(account["provider_id"], {
                    "account": account, "mode": "refresh", "secrets": params.get("secrets", {})})
            except Exception as exc:
                self.store.update_account_refresh(account["id"], "failed", error=_redact_diagnostic(str(exc)))
                raise
            if isinstance(result, dict):
                for key in ("expires_at", "quota_bytes", "quota_reset_at", "health"):
                    if key in result:
                        account[key] = result[key]
            account["state"], account["last_error"], account["refresh_state"] = "healthy", None, "active"
            self.store.save_account(account)
            return next(item for item in self.store.list_accounts(account["provider_id"]) if item["id"] == account["id"])
        if method == "retry_decision":
            policy = RetryPolicy(**{key: value for key, value in (params.get("policy") or {}).items() if key in RetryPolicy.__dataclass_fields__})
            return decide_retry(params.get("error", ""), int(params.get("attempt", 0)), policy,
                                params.get("status_code"), params.get("retry_after")).to_dict()
        if method == "classify_failure":
            return {"failure": classify_failure(params.get("error", ""), params.get("status_code")).value}
        if method == "verify_download":
            report = verify_file(params["path"], expected_size=params.get("expected_size"),
                                 expected_checksum=params.get("expected_checksum"), algorithm=params.get("algorithm", "sha256"),
                                 content_type=params.get("content_type"), etag=params.get("etag"),
                                 last_modified=params.get("last_modified"))
            if params.get("task_id"):
                task = self.store.get(params["task_id"])
                if task:
                    task.integrity = report.to_dict()
                    self.store.save(task)
            return report.to_dict()
        if method == "migration_status":
            return self.store.migration_status()
        if method == "list_notification_sinks":
            return self.store.list_notification_sinks()
        if method == "delete_notification_sink":
            sink_id = str(params["id"])
            if not self.store.delete_notification_sink(sink_id):
                raise KeyError("notification sink not found")
            return {"deleted": sink_id}
        if method == "save_notification_sink":
            sink = dict(params)
            if not sink.get("id") or sink.get("kind") not in {"webhook", "desktop"} or not sink.get("endpoint"):
                raise ValueError("sink id, kind, and endpoint are required")
            self.store.save_notification_sink(sink)
            return next(item for item in self.store.list_notification_sinks() if item["id"] == sink["id"])
        if method == "flush_notifications":
            delivered_count, failed_count = 0, 0
            notification_settings = self._ui_settings().get("notifications", {})
            allowed_events = {
                "TaskCompleted": bool(notification_settings.get("notifComplete", True)),
                "TaskFailed": bool(notification_settings.get("notifFailed", True)),
                "TaskPaused": bool(notification_settings.get("notifPause", False)),
            }
            for event in self.store.pending_deliveries(int(params.get("limit", 100))):
                if event["event_type"] in allowed_events and not allowed_events[event["event_type"]]:
                    self.store.record_delivery(event["id"], event["sink_id"], "delivered")
                    if not self.store.event_has_pending_delivery(event["id"]):
                        self.store.mark_event_published(event["id"])
                    continue
                if event["event_types"] and event["event_type"] not in event["event_types"]:
                    self.store.record_delivery(event["id"], event["sink_id"], "delivered")
                    if not self.store.event_has_pending_delivery(event["id"]):
                        self.store.mark_event_published(event["id"])
                    continue
                try:
                    event["notification_sound"] = bool(notification_settings.get("notifSound", True))
                    event["sound_preset"] = notification_settings.get("soundPreset", "Windows Notify")
                    sink = {key: event.get(key) for key in ("kind", "endpoint", "credential_ref")}
                    deliver(sink, event)
                    self.store.record_delivery(event["id"], event["sink_id"], "delivered")
                    delivered_count += 1
                except Exception as exc:
                    self.store.record_delivery(event["id"], event["sink_id"], "failed", str(exc)[:500])
                    failed_count += 1
                if not self.store.event_has_pending_delivery(event["id"]):
                    self.store.mark_event_published(event["id"])
            return {"delivered": delivered_count, "failed": failed_count}
        if method == "clear_completed_tasks":
            completed_tasks = [task for task in self.store.list() if task.state == "completed"]
            completed = [task.id for task in completed_tasks]
            for task_id in completed:
                self.store.delete(task_id)
            if hasattr(self, "_prune_multipart_probe_state"):
                for task in completed_tasks:
                    is_mp, pkey, _ = self._task_multipart_info(task)
                    if is_mp and pkey:
                        self._prune_multipart_probe_state(pkey)
            return {"deleted": len(completed), "affected_ids": completed}
        if method in {"browser_capture_batch", "capture_batch"}:
            return self._capture_batch(params)
        if method in {"capture_review_list", "capture_inbox_list"}:
            from .capture import with_file_kinds
            batches = self.store.list_capture_batches(int(params.get("limit", 100)))
            if not params.get("include_dismissed"):
                batches = [b for b in batches if b.get("state") != "dismissed"]
            blocked: set[str] = set()
            if self._ui_settings()["network"].get("adblock", True):
                urls = [str(c.get("url") or "") for b in batches for c in (b.get("candidates") or []) if isinstance(c, dict)]
                blocked = self._adblock().blocked_urls([u for u in urls if u])
            return {"batches": [with_file_kinds(batch, blocked) for batch in batches]}
        if method == "capture_dismiss":
            # Removing a capture from Explore / the banner. Kept in the store (as
            # "dismissed") so a re-sent capture is still recognised as seen.
            ids = [str(i) for i in params.get("batch_ids", [])]
            for batch_id in ids:
                self.store.update_capture_batch(batch_id, state="dismissed", acknowledged=True)
            return {"dismissed": ids}
        if method in {"capture_review_get", "capture_batch_get"}:
            result = self.store.get_capture_batch(batch_id=params.get("batch_id"), request_id=params.get("request_id"))
            if result is None:
                raise KeyError("capture batch not found")
            return result
        if method in {"capture_import", "capture_review_import"}:
            return self._capture_import(params)
        if method == "browser_import":
            if params.get("batch_id"):
                return self._capture_import(params)
            capture = validate_capture(params)
            candidates = params.get("candidates") or []
            if candidates:
                ranked = rank_candidates(candidates, page_url=capture["url"])
                if not ranked:
                    raise ValueError("browser capture did not contain a usable candidate")
                chosen = ranked[0]
                capture = {**capture, **public_candidate(chosen), "url": chosen.url,
                           "display_name": chosen.filename or capture.get("display_name")}
            task_params = {"url": capture["url"], "destination": params.get("destination"),
                           "display_name": capture.get("display_name"), "priority": params.get("priority", 0),
                           "queue_id": params.get("queue_id", "default"), "category": params.get("category"),
                           "duplicate_strategy": params.get("duplicate_strategy", "skip"),
                           "request_headers": capture["headers"], "referrer": capture.get("referrer"),
                           "browser_context": {**capture.get("page_context", {}),
                                               "credential_ref": capture.get("credential_ref"),
                                               "capture_source": "browser"}}
            return self.dispatch("add_task", task_params)
        if method in {"capture_candidates", "browser_capture"}:
            values = list(params.get("candidates") or [])
            if params.get("url"):
                values.append({"url": params["url"], "headers": params.get("headers", {}),
                               "referrer": params.get("referrer"), "credential_ref": params.get("credential_ref"),
                               "mime": params.get("mime"), "filename": params.get("display_name"),
                               "confidence": params.get("confidence", 0.2)})
            ranked = rank_candidates(values, page_url=params.get("page_url"), limit=int(params.get("limit", 128)))
            result = {"candidates": [public_candidate(item) for item in ranked]}
            if params.get("enqueue") and ranked:
                result["task"] = self.dispatch("browser_import", {**params, **public_candidate(ranked[0]),
                                                                      "url": ranked[0].url, "candidates": []})
            return result
        if method in {"inspect_candidates", "resolve_page"}:
            url = _normalize_input_url(params["url"])
            return self.universal_resolver.inspect(url, headers=params.get("headers"))
        if method == "media_extract":
            url = _normalize_input_url(params["url"])
            result = self.universal_resolver.media_extractor.extract(url)
            if not result:
                raise ValueError("media extraction returned no stream")
            return {
                "source_url": result["source_url"],
                "title": result["title"],
                "duration": result["duration"],
                "thumbnail": result["thumbnail"],
                "selected_url": result["selected_url"],
                "formats": result.get("formats", []),
                "resolved_item": result["resolved_item"].to_dict(),
            }
        if method == "media_metadata":
            return self.plugins.media_metadata(params["url"])
        if method == "helper_invoke":
            return self._helper_invoke(params)
        if method == "helper_status":
            return self._helper_status(params)
        if method == "helper_cancel":
            return self._helper_cancel(params)
        if method == "media_plan":
            url = _normalize_input_url(params["url"])
            metadata = self.plugins.media_metadata(url)
            if metadata.get("variants") and not metadata.get("segments"):
                selected = metadata.get("selected_variant") or metadata["variants"][0]
                variant_url = selected.get("url")
                if variant_url:
                    import urllib.request
                    with route_http.urlopen(urllib.request.Request(variant_url), timeout=20) as response:
                        body = response.read(8 * 1024 * 1024).decode("utf-8", errors="replace")
                    metadata = parse_media(variant_url, body).to_dict()
            return metadata
        if method == "media_assemble":
            raw = params.get("plan") or self.dispatch("media_plan", {"url": params["url"]})
            segments = [MediaSegment(**item) for item in raw.get("segments", [])]
            plan = MediaPlan(raw["kind"], raw["manifest_url"] if "manifest_url" in raw else raw["url"],
                             segments=segments, variants=raw.get("variants", []),
                             selected_variant=raw.get("selected_variant"), encrypted=bool(raw.get("encrypted")),
                             output_format=raw.get("output_format"), total_duration=raw.get("total_duration"))
            return self.media_assembler.assemble(plan, params["output"],
                                                  ffmpeg=params.get("ffmpeg"),
                                                  progress=lambda done, total: self.events.emit("MediaProgress", params.get("task_id"), {"done": done, "total": total}) if params.get("task_id") else None)
        if method == "extract_links":
            return [item.to_dict() for item in self.plugins.extract_links(_normalize_input_url(params["url"]), params.get("secrets"))]
        if method == "follow_download":
            # In the browser, on the active route, with ads blocked (button_follower).
            from .browser_solver import solver_daemon
            return solver_daemon.follow_download_sync(_normalize_input_url(params["url"]),
                                                      timeout_seconds=float(params.get("timeout", 150)))
        if method == "resolve_chain":
            return [item.to_dict() for item in self.plugins.resolve_chain(_normalize_input_url(params["url"]), params.get("secrets"), int(params.get("max_hops", 8)))]
        if method == "shortlink_catalog":
            from .shortlinks import load_catalog
            catalog = load_catalog()
            return {"version": catalog.get("version", 1), "count": len(catalog.get("entries", [])),
                    "entries": catalog.get("entries", [])}
        if method == "list_shortlink_chain":
            return self.store.list_shortlink_chain(params["task_id"])

        if method == "operational_report":
            from .observability import OperationalAggregator
            aggregator = OperationalAggregator(self)
            report = aggregator.generate_report(
                task_id=params.get("task_id"),
                run_fixture_benchmark=bool(params.get("run_benchmark", False)),
            )
            return report.to_dict()
        if method == "diagnose_task":
            task = self.store.get(params["task_id"])
            if not task:
                raise KeyError("task not found")
            return {
                "task": _redact_diagnostic({
                    "id": task.id, "source_url": task.source_url, "state": task.state,
                    "provider": task.provider, "size": task.size, "completed_bytes": task.completed_bytes,
                    "error": task.error, "retry_count": task.retry_count, "user_action": task.user_action,
                    "user_challenge": task.user_challenge, "paused_reason": task.paused_reason,
                    "category": task.category, "folder_path": task.folder_path,
                    "speed_bytes_per_second": task.speed_bytes_per_second,
                    "average_speed_bytes_per_second": task.average_speed_bytes_per_second,
                    "eta_seconds": task.eta_seconds, "backend": task.backend,
                    "attempt_count": task.attempt_count, "integrity_state": task.integrity_state,
                }),
                "shortlink_chain": self.store.list_shortlink_chain(task.id),
                "provider_attempts": self.store.list_provider_attempts(task.id),
                "route_attempts": self.store.list_route_attempts(task.id),
                "segments": self.store.list_segments(task.id),
                "hashes": self.store.list_hashes(task.id),
                "resolved_items": [_redact_diagnostic({
                    "provider": item.provider, "source_url": item.source_url,
                    "display_name": item.display_name, "relative_path": item.relative_path,
                    "size": item.size, "direct_url": item.direct_url,
                    "metadata": item.metadata,
                }) for item in task.resolved],
                "logs": self.store.list_diagnostics(task.id),
                "recommendations": self._diagnostic_recommendations(task),
            }
        if method == "enumerate":
            return [item.to_dict() for item in self.plugins.enumerate(_normalize_input_url(params["url"]), params.get("secrets"))]
        if method == "protocol_resolve":
            return [item.to_dict() for item in resolve_protocol(params["url"], credential_ref=params.get("credential_ref"))]
        if method == "resolution_resolve":
            context = ResolutionContext(**{key: params[key] for key in ResolutionContext.__dataclass_fields__ if key in params})
            account = next((item for item in self.store.list_accounts(context.provider_id)
                            if item["id"] == context.account_ref), None) if context.account_ref else None
            if context.account_ref and not account:
                raise KeyError("account not found")
            if account and (not account.get("enabled", True) or account.get("state") in {"disabled", "quarantined"}):
                raise ProviderMappedError("account is not admitted", "admission_rejected")
            credential_ref = account.get("credential_ref") if account else params.get("credential_ref")
            supplied = params.get("secrets")
            def credentials(_ref):
                if supplied:
                    return dict(supplied)
                value = self.secrets.resolve_operation(credential_ref)
                return {"credential": value} if value else {}
            refreshed = {"done": False}
            def refresh(source, operation_credentials):
                provider = context.provider_id or (account or {}).get("provider_id") or self.plugins.provider_for(source)
                result = self.plugins.authenticate(provider, {"account": self._account_public(account or {}),
                                                               "mode": "refresh", "secrets": operation_credentials})
                refreshed["done"] = True
                return result if result is not None else True
            items = self.resolution_broker.resolve_with_account(
                context,
                lambda source, operation_credentials: self.plugins.resolve_chain(source, operation_credentials, 8),
                credentials,
                refresh=refresh if account else None,
                on_refresh=lambda reason: self.events.emit("ProviderRefresh", None, {
                    "provider_id": context.provider_id, "account_ref": context.account_ref,
                    "reason": reason, "outcome": "refreshed"},
                    f"refresh:{context.account_ref}:{reason}"),
            )
            if params.get("enqueue") and items:
                queued = self.dispatch("add_task", {"url": items[0].source_url,
                                                       "destination": params.get("destination"),
                                                       "display_name": items[0].display_name,
                                                       "account_ref": context.account_ref,
                                                       "category": "provider"})
                return {"items": [public_item(item) for item in items], "task": queued}
            return [public_item(item) for item in items]
        if method == "resolution_current":
            context = ResolutionContext(**{key: params[key] for key in ResolutionContext.__dataclass_fields__ if key in params})
            return [item.to_dict() for item in self.resolution_broker.current(context)]
        if method == "session_register":
            return self.session_broker.register(params["reference"], params["provider_id"], params.get("expires_at"), params.get("account_ref"))
        if method == "session_status":
            return self.session_broker.status(params["reference"])
        if method == "session_refresh":
            return self.session_broker.refresh(params["reference"], expires_at=params.get("expires_at"),
                                               state=params.get("state", "active"))
        if method in {"container_import", "import_container", "crawl_import"}:
            idem = params.get("idempotency_key")
            idem_hash = request_hash(params) if idem else None
            if idem:
                cached = self.store.get_idempotency_record(str(idem), method)
                if cached is not None:
                    if cached["request_hash"] != idem_hash:
                        raise ValueError("idempotency key was reused with different parameters")
                    return json.loads(cached["response_json"])
            payload = params.get("content", params.get("data"))
            if payload is None and params.get("path"):
                payload = Path(params["path"])
            if payload is None:
                raise ValueError("container import requires content, data, or path")
            imported = normalize_import(payload, name=params.get("name"), format=params.get("format"),
                                        container_id=params.get("container_id"),
                                        max_bytes=int(params.get("max_bytes", 4 * 1024 * 1024)),
                                        max_entries=int(params.get("max_entries", 10000)))
            crawl_id = imported.collection_key
            raw = self.store.get_collection_plan(crawl_id)
            nodes, events = records_as_graph(imported, params.get("provider_id", "generic"))
            if raw is not None:
                existing_ids = {node["node_id"] for node in raw.get("nodes", [])}
                nodes = raw.get("nodes", []) + [node for node in nodes if node["node_id"] not in existing_ids]
                existing_events = raw.get("events", [])
                events = existing_events + [event for event in events if event not in existing_events]
                prior_items = raw.get("items", [])
            else:
                prior_items = []
            from .models import CollectionItem
            item_ids = {item["stable_id"] for item in prior_items}
            items = list(prior_items)
            for node in nodes:
                if node.get("status") == "container":
                    continue
                stable_id = node["node_id"]
                if stable_id in item_ids:
                    continue
                items.append(CollectionItem(stable_id, node["source_url"], node["display_name"],
                                             node.get("folder_path", ""), node.get("size"), False,
                                             node.get("status", "discovered"), None, node.get("metadata", {}),
                                             node.get("parent_id"), int(node.get("depth", 0)), 0, None,
                                             node.get("folder_path", ""), node.get("package_path", ""),
                                             node.get("outcome")).to_dict())
                item_ids.add(stable_id)
            plan = CollectionPlan(crawl_id, "container://" + imported.container_id, params.get("provider_id", "generic"),
                                  [], max_items=int(params.get("max_entries", 10000)), state="complete",
                                  outcome="success", graph_revision=(int(raw.get("graph_revision", 0)) if raw else 0) + 1,
                                  nodes=nodes, events=events)
            plan.items = [CollectionItem(**item) for item in items]
            self.store.save_collection_plan(plan)
            self.events.emit("CrawlSnapshot", plan.id, {"crawl_id": plan.id, "revision": plan.graph_revision,
                                                         "state": plan.state, "outcome": plan.outcome,
                                                         "source_format": imported.format, "nodes": redact(nodes)},
                             f"crawl:{plan.id}:{plan.graph_revision}")
            result = {**plan.to_dict(), "import": {"format": imported.format, "container_id": imported.container_id,
                                                    "source_name": imported.source_name, "imported_count": len(imported.records),
                                                    "new_count": len(items) - len(prior_items), "deduplicated_count": len(imported.records) - len(items) + len(prior_items)}}
            if idem:
                self.store.save_idempotent(str(idem), method, idem_hash, result)
            return result
        if method in {"crawl_snapshot", "crawl_list", "crawl_continue", "crawl_inspect", "crawl_status"}:
            aliases = {"crawl_snapshot": "crawl_get", "crawl_list": "list_collection_plans",
                       "crawl_continue": "collection_next_page", "crawl_inspect": "crawl_get", "crawl_status": "crawl_get"}
            return self.dispatch(aliases[method], params)
        if method == "collection_create":
            source_url = _normalize_input_url(params["url"])
            raw_items = params.get("items")
            crawl_id = params.get("id", str(uuid4()))
            provider_id = params.get("provider_id")
            if raw_items is not None:
                # Fixture/test callers can provide one deterministic provider page;
                # the planner still owns graph creation and all persistence.
                fixture_page = {"used": False}
                def fetch(_url, _secrets):
                    if fixture_page["used"]:
                        return []
                    fixture_page["used"] = True
                    return raw_items
            else:
                fetch = lambda url, secrets: self.plugins.enumerate(url, secrets)
            plan = self.collection_planner.crawl(crawl_id, source_url, provider_id, fetch,
                                                 max_depth=params.get("max_depth"), max_pages=params.get("max_pages"),
                                                 max_items=params.get("max_items"), cursor=params.get("cursor"),
                                                 cancel=lambda: False, secrets=params.get("secrets"))
            self.store.save_collection_plan(plan)
            self.events.emit("CrawlSnapshot", plan.id, {"crawl_id": plan.id, "revision": plan.graph_revision,
                                                         "state": plan.state, "outcome": plan.outcome,
                                                         "nodes": redact(plan.nodes)}, f"crawl:{plan.id}:{plan.graph_revision}")
            return plan.to_dict()
        if method == "collection_get":
            result = self.store.get_collection_plan(params["id"])
            if result is None:
                raise KeyError("collection plan not found")
            return result
        if method == "collection_select":
            raw = self.store.get_collection_plan(params["id"])
            if raw is None:
                raise KeyError("collection plan not found")
            plan = CollectionPlan(**{**raw, "items": []})
            from .models import CollectionItem
            plan.items = [CollectionItem(**item) for item in raw.get("items", [])]
            plan = self.collection_planner.select(plan, params.get("item_ids", []), bool(params.get("selected", True)))
            self.store.save_collection_plan(plan)
            return plan.to_dict()
        if method == "collection_next_page":
            raw = self.store.get_collection_plan(params["id"])
            if raw is None:
                raise KeyError("collection plan not found")
            plan = CollectionPlan(**{**raw, "items": []})
            from .models import CollectionItem
            plan.items = [CollectionItem(**item) for item in raw.get("items", [])]
            if plan.state in {"canceled", "complete"} or plan.outcome in {"canceled", "quota"} and not plan.has_more:
                return plan.to_dict()
            page_items = params.get("items")
            if page_items is None:
                page_items = self.plugins.enumerate(plan.source_url, params.get("secrets"))
            plan = self.collection_planner.append(plan, page_items, cursor=params.get("cursor"),
                                                  has_more=bool(params.get("has_more", False)))
            self.store.save_collection_plan(plan)
            return plan.to_dict()
        if method == "collection_item_status":
            raw = self.store.get_collection_plan(params["id"])
            if raw is None:
                raise KeyError("collection plan not found")
            plan = CollectionPlan(**{**raw, "items": []})
            from .models import CollectionItem
            plan.items = [CollectionItem(**item) for item in raw.get("items", [])]
            plan = self.collection_planner.update_status(plan, params["item_id"], params["status"], params.get("error"))
            self.store.save_collection_plan(plan)
            return plan.to_dict()
        if method == "list_collection_plans":
            return self.store.list_collection_plans()
        if method == "collection_enqueue":
            raw = self.store.get_collection_plan(params["id"])
            if raw is None:
                raise KeyError("collection plan not found")
            if raw.get("state") == "canceled" or raw.get("outcome") == "canceled":
                return {"collection_id": params["id"], "queued": [], "outcome": "canceled"}
            destination = params.get("destination")
            items_by_id = {item["stable_id"]: item for item in raw.get("items", [])}
            requested = params.get("item_ids")
            selected = list(requested if requested is not None else
                            [item["stable_id"] for item in raw.get("items", []) if item.get("selected")])
            unknown = [item_id for item_id in selected if item_id not in items_by_id]
            if unknown:
                raise ValueError(f"stale or unknown collection item IDs: {', '.join(map(str, unknown))}")
            if len(set(selected)) != len(selected):
                raise ValueError("collection item IDs must be unique")
            candidates = [items_by_id[item_id] for item_id in selected]
            parent_ids = {node.get("parent_id") for node in raw.get("nodes", []) if node.get("parent_id")}
            containers = [item["stable_id"] for item in candidates if item["stable_id"] in parent_ids]
            if containers:
                raise ValueError(f"collection parent nodes are not resolved items: {', '.join(containers)}")
            blocked = [item["stable_id"] for item in candidates if item.get("status") in {"queued", "completed"}]
            if blocked:
                raise ValueError(f"collection items are no longer enqueueable: {', '.join(blocked)}")
            created = []
            for item in candidates:
                task = self.dispatch("add_task", {"url": item["source_url"], "destination": destination,
                                                   "display_name": item.get("display_name"),
                                                   "category": "collection", "account_ref": params.get("account_ref"),
                                                   "queue_id": params.get("queue_id", "default"),
                                                   "selected_item_ids": [item["stable_id"]],
                                                   "browser_context": {"collection_id": params["id"],
                                                                        "collection_item_id": item["stable_id"]}})
                self.dispatch("collection_item_status", {"id": params["id"], "item_id": item["stable_id"], "status": "queued"})
                created.append(task)
            return {"collection_id": params["id"], "queued": created}
        if method == "provider_failure":
            """Persist a provider outcome without allowing it to reactivate a terminal task."""
            policy = classify_provider_failure(params.get("category") or params.get("message", ""),
                                               status_code=params.get("status_code"),
                                               retry_after=params.get("retry_after"))
            secret_values: dict[str, str] = {}
            for key, value in (params.get("challenge") or {}).items():
                if any(marker in str(key).lower() for marker in ("token", "cookie", "secret", "password", "authorization", "signature")):
                    if isinstance(value, (str, int, float)) and value:
                        secret_values[str(key)] = str(value)
            safe_message = str(redact(params.get("message", policy.category), secret_values))[:500]
            safe_challenge = _redact_diagnostic(redact(params.get("challenge") or {}, secret_values))
            task_id = params.get("task_id")
            task = self.store.get(task_id) if task_id else None
            if task:
                if task.state not in {"completed", "failed", "canceled"}:
                    if task.state == "needs_user" and not policy.user_action:
                        # Preserve an outstanding user-action gate. A later
                        # provider observation must not silently turn it into
                        # a terminal failure or reactivate the task.
                        pass
                    elif policy.user_action:
                        task.user_action = policy.user_action
                        task.user_challenge = safe_challenge
                        self._transition(task, "needs_user", "ProviderActionRequired", safe_message)
                    else:
                        self._transition(task, "failed", "ProviderFailed", safe_message)
                result = {"task_id": task.id, "state": task.state, "category": policy.category,
                          "retryable": policy.retryable, "retry_after": policy.retry_after,
                          "user_action": policy.user_action}
            else:
                result = {"task_id": task_id, "state": None, "category": policy.category,
                          "retryable": policy.retryable, "retry_after": policy.retry_after,
                          "user_action": policy.user_action}
            collection_id, node_id = params.get("collection_id"), params.get("node_id")
            if collection_id and node_id:
                status = policy.category
                if status == "expired_source":
                    status = "refresh_required"
                raw = self.store.get_collection_plan(collection_id)
                if raw is not None and any(item.get("stable_id") == node_id for item in raw.get("items", [])):
                    self.dispatch("collection_item_status", {"id": collection_id, "item_id": node_id,
                                                               "status": status, "error": safe_message})
                    self.events.emit("ProviderOutcome", collection_id,
                                     {"collection_id": collection_id, "node_id": node_id,
                                      "category": policy.category, "state": status,
                                      "message": safe_message, "user_action": policy.user_action},
                                     f"provider:{collection_id}:{node_id}:{policy.category}")
                    result.update({"collection_id": collection_id, "node_id": node_id, "state": status})
            return result
        if method == "provider_admission":
            provider_id = params.get("provider_id")
            account_id = params.get("account_id") or params.get("account_ref")
            account = next((item for item in self.store.list_accounts(provider_id) if item["id"] == account_id), None) if account_id else None
            if account_id and account is None:
                return {"admitted": False, "category": "admission_rejected", "reason": "account not found"}
            if account and (not account.get("enabled", True) or account.get("state") in {"disabled", "quarantined"}):
                return {"admitted": False, "category": "admission_rejected", "reason": "account is not admitted"}
            health = next((item for item in self.health_monitor.snapshots() if item.get("provider_id") == provider_id), None)
            if health and health.get("quarantined"):
                return {"admitted": False, "category": "admission_rejected", "reason": "provider is quarantined"}
            return {"admitted": True, "provider_id": provider_id, "account_id": account_id}
        if method == "collection_cancel":
            raw = self.store.get_collection_plan(params["id"])
            if raw is None:
                raise KeyError("collection plan not found")
            plan = CollectionPlan(**{**raw, "items": []})
            from .models import CollectionItem
            plan.items = [CollectionItem(**item) for item in raw.get("items", [])]
            plan.state = "canceled"
            plan.outcome = "canceled"
            plan.graph_revision += 1
            for item in plan.items:
                if item.status not in {"completed", "failed"}:
                    item.status = "canceled"
            self.store.save_collection_plan(plan)
            self.events.emit("CrawlOutcome", plan.id, {"crawl_id": plan.id, "revision": plan.graph_revision,
                                                        "state": plan.state, "outcome": plan.outcome}, f"crawl:{plan.id}:canceled")
            return plan.to_dict()
        if method in {"crawl_get", "collection_graph_get"}:
            raw = self.store.get_collection_plan(params["id"])
            if raw is None:
                raise KeyError("crawl not found")
            return {**raw, "nodes": self.store.list_crawl_nodes(params["id"]),
                    "events": self.store.list_crawl_events(params["id"])}
        if method in {"crawl_events", "collection_graph_events"}:
            return self.store.list_crawl_events(params["id"])
        if method == "crawl_cancel":
            return self.dispatch("collection_cancel", {"id": params["id"]})
        if method == "provider_health_snapshot":
            return self.health_monitor.snapshots()
        if method == "provider_record_outcome":
            return self.health_monitor.record(params["provider_id"], params.get("outcome", "failed"),
                                              params.get("category"), params.get("version"), params.get("error")).to_dict()
        if method == "provider_canary_status":
            return {"snapshots": self.health_monitor.snapshots(), "plugins": self.plugins.list_health()}
        if method == "provider_contract_test":
            from .plugin_contracts import validate_plugin_contract
            return run_contract_suite(params["provider_dir"], checker=validate_plugin_contract)
        if method == "provider_fixture_generate":
            return generate_fixture_bundle(params.get("exchanges", []), params.get("provider_id"))
        if method == "provider_canary_run":
            if not params.get("opt_in", False):
                return {"ran": False, "reason": "live canaries require explicit opt_in"}
            results = []
            for provider_id in self.plugins.order:
                try:
                    result = self.plugins._call(provider_id, "health", {})
                    results.append({"provider_id": provider_id, "healthy": bool((result or {}).get("healthy", True))})
                except Exception as exc:
                    results.append({"provider_id": provider_id, "healthy": False, "error": str(exc)[:500]})
            return {"ran": True, "results": results, "snapshots": self.health_monitor.snapshots()}
        if method == "provider_catalog_sign":
            return sign_catalog(params.get("catalog", {}), params["secret"])
        if method == "provider_catalog_verify":
            return verify_catalog(params["signed"], params["secret"])
        if method == "list_hashes":
            return self.store.list_hashes(params["task_id"])
        if method == "get_download_directory":
            value = self.store.get_setting("download_directory", str(self.data_dir / "downloads"))
            return validate_directory(value, self.data_dir, create=False)
        if method == "set_download_directory":
            value = effective_directory(params.get("directory"), self.data_dir, create=True)
            self.store.set_setting("download_directory", value)
            return validate_directory(value, self.data_dir, create=False)
        if method == "validate_download_directory":
            return validate_directory(params.get("directory", ""), self.data_dir, bool(params.get("create", False)))
        if method == "get_archive_directory":
            value = self.store.get_setting("archive_directory", str(self.data_dir / "archives"))
            return validate_directory(value, self.data_dir, create=False)
        if method == "set_archive_directory":
            value = effective_directory(params.get("directory"), self.data_dir, create=True)
            self.store.set_setting("archive_directory", value)
            return validate_directory(value, self.data_dir, create=False)
        if method in {"list_route_profiles", "routes_list", "route_list"}:
            return self.routes.profiles()
        if method in {"save_route_profile", "route_save"}:
            saved = self.routes.save(params)
            self._sync_active_route()
            return saved
        if method in {"delete_route_profile", "route_delete"}:
            doomed = next((p for p in self.routes.profiles() if p["id"] == params["profile_id"]), None)
            deleted = self.routes.delete(params["profile_id"])
            if doomed and doomed.get("kind") == "wireguard":
                self.tunnels.stop(doomed["id"])
            ref = (doomed or {}).get("credential_ref")
            # Locations of one account share its key: it goes with the last of them.
            if ref and not any(p.get("credential_ref") == ref for p in self.routes.profiles()):
                self.secrets.delete(ref)
            self._sync_active_route()
            return deleted
        if method == "route_save_wireguard":
            # The whole .conf (private key included) goes to the secret store;
            # the profile keeps only a reference to it.
            parsed = self.routes.parse_wireguard_conf(params["conf_text"])
            existing = next((p for p in self.routes.profiles() if p["id"] == params["id"]), None)
            if existing and existing.get("credential_ref"):
                credential_ref = self.secrets.rotate(existing["credential_ref"], params["conf_text"])
            else:
                credential_ref = self.secrets.put(params["conf_text"], kind="wireguard_conf")
            self.tunnels.stop(params["id"])  # new keys or server: the next use starts fresh
            saved = self.routes.save({
                "id": params["id"], "kind": "wireguard", "endpoint": parsed["endpoint"],
                "region": params.get("region"), "enabled": True, "credential_ref": credential_ref,
                "tunnel": params.get("tunnel") or "core",
            })
            self._sync_active_route()
            return saved
        if method == "adblock_status":
            return self._adblock().status()
        if method == "adblock_update":
            return self._adblock().update()
        if method == "tunnel_status":
            return self.tunnels.status()
        if method in {"provider_account_login", "provider_account_refresh", "provider_account_logout"}:
            return self._mullvad_account(method, params)
        if method == "route_health":
            return self.routes.health(params["profile_id"])
        if method in {"fetch_public_proxies", "routes_fetch_public_proxies"}:
            from .routes import fetch_public_proxies
            return fetch_public_proxies(limit=params.get("limit", 50), protocol=params.get("protocol"))
        if method == "urls_extract":
            # One parser for every place links are typed or pasted (scheme-less included).
            return {"urls": extract_urls(str(params.get("text", "")))}
        if method == "favicons_get":
            return {"icons": self._favicons().get_many([str(h) for h in params.get("hosts", [])])}
        if method == "route_next_healthy":
            # "Next best" in Connections: same ranking and settings as quota switching.
            route_settings = self._ui_settings().get("routes", {})
            nxt = self.routes.next_healthy_route(
                "ui:next-best", current_route_id=params.get("current"),
                include_proxies=bool(params.get("include_proxies", route_settings.get("switchIncludesProxies", True))),
                allow_direct=bool(params.get("allow_direct", route_settings.get("allowDirectFallback", True))),
            )
            from dataclasses import asdict
            return {"route": asdict(nxt) if nxt else None}
        if method in {"test_proxy_download", "routes_test_proxy_download"}:
            from .routes import test_proxy_download, with_proxy_credentials
            endpoint = params["endpoint"]
            if params.get("credential_ref"):
                endpoint = with_proxy_credentials(endpoint, self.secrets.resolve_operation(params["credential_ref"]))
            return test_proxy_download(endpoint, params.get("kind", "http_proxy"))
        if method == "record_route_attempt":
            return self.routes.record_attempt(params["task_id"], params["profile_id"], params["outcome"], params.get("reason"))
        if method == "list_route_attempts":
            return self.store.list_route_attempts(params["task_id"])
        if method == "next_route_profile":
            return {"profile_id": self.routes.next_profile(params["task_id"], params.get("candidates", []))}
        if method == "route_switch_allowed":
            return {"allowed": self.routes.automatic_switch_allowed(params.get("error", ""), params.get("status_code"), params.get("category"))}
        if method == "list_provider_attempts":
            return self.store.list_provider_attempts(params["task_id"], params.get("source_fingerprint"))
        if method == "record_provider_attempt":
            self.fallback.record(params["task_id"], params["url"], params.get("provider_id", "generic"),
                                 params.get("route_profile_id", "direct"), params["outcome"],
                                 params.get("status_code"), params.get("retry_after"), params.get("error"))
            return {"recorded": True}
        if method == "select_alternate_source":
            task = self.store.get(params["task_id"])
            alternatives = params.get("alternatives") if "alternatives" in params else (task.alternate_urls if task else [])
            return self.fallback.candidates(params["task_id"], alternatives or [], params.get("route_profile_id", "direct"))
        if method == "engine_info":
            return {
                "custom_async": True,
                "rust_core": self.rust_backend.available(),
                "archive_worker": self.archive_backend.available(),
                "policy": {
                    "max_active_tasks": self.resources.policy.max_active_tasks,
                    "max_active_segments": self.resources.policy.max_active_segments,
                    "per_host_transfers": self.resources.policy.per_host_transfers,
                    "per_host_requests_per_second": self.resources.policy.per_host_requests_per_second,
                    "per_provider_transfers": self.resources.policy.per_provider_transfers,
                    "per_account_transfers": self.resources.policy.per_account_transfers,
                    **self.resources.policy.to_dict(),
                },
            }
        if method in {"list_events", "events_since"}:
            after = params.get("after_id") if "after_id" in params else params.get("event_id", params.get("cursor"))
            if after is not None:
                events = self.store.events_since(int(after), int(params.get("limit", 100)))
            else:
                events = self.events.pending(int(params.get("limit", 100)))
            if params.get("task_id"):
                events = [event for event in events if event.get("task_id") == params["task_id"]]
            if params.get("event_type"):
                events = [event for event in events if event.get("event_type") == params["event_type"]]
            return self._public_events(events)
        if method in {"ack_event", "event_acknowledge"}:
            eid = params.get("id") if params.get("id") is not None else params.get("event_id")
            if eid is not None:
                event_id = int(eid)
                self.events.acknowledge(event_id)
                return {"acknowledged": event_id}
            if "event_ids" in params and isinstance(params["event_ids"], list):
                for single in params["event_ids"]:
                    self.events.acknowledge(int(single))
                return {"acknowledged": [int(x) for x in params["event_ids"]]}
            raise ValueError("event id is required")
        if method == "list_archive_jobs":
            return self.store.list_archive_jobs(params.get("task_id"))
        if method in {"archive_extract_job", "archive_enqueue_extract"}:
            path = Path(params["path"]).expanduser().resolve(strict=True)
            if not path.is_file():
                raise ValueError("archive input must be a file")
            task_id = params.get("task_id")
            task = self.store.get(task_id) if task_id else None
            if task_id and not task:
                raise KeyError("task not found")
            if task:
                task_root = Path(task.destination).resolve()
                if path != task_root and task_root not in path.parents:
                    raise ValueError("archive input is outside the task destination")
            output_value = params.get("output_directory") or self.store.get_setting(
                "archive_directory", str(self.data_dir / "archives"))
            output = Path(effective_directory(str(output_value), self.data_dir, create=True))
            job_id = str(params.get("job_id") or uuid4())
            package_key = hashlib.sha256(f"extract|{task_id or ''}|{path}|{output}".encode("utf-8")).hexdigest()
            job = ArchiveJob(
                job_id, task_id or "", str(path), str(output), None, task.password_ref if task else params.get("password_ref"),
                params.get("policy") or self.resources.policy.to_dict(), "queued", package_key=package_key,
                operation="extract", expected_digest=params.get("expected_digest"),
            )
            persisted, created = self.store.insert_or_get_archive_job(job)
            if created:
                self._archive_event("ArchiveQueued", persisted)
            if persisted.state == "queued":
                self._schedule_archive_job(persisted.id)
            return persisted.to_dict()
        if method == "retry_archive_job":
            job_id = str(params["job_id"])
            job = self.store.get_archive_job(job_id)
            if not job:
                raise KeyError("archive job not found")
            if job.state not in {"failed", "canceled", "needs_user"}:
                raise ValueError(f"archive job in state {job.state} cannot be retried")
            queued = self.store.transition_archive_job(
                job_id, "queued", expected_revision=params.get("expected_revision"))
            self._archive_controls.pop(job_id, None)
            self._archive_event("ArchiveQueued", queued, reason="explicit_retry")
            self._schedule_archive_job(job_id)
            return queued.to_dict()
        if method == "cancel_archive_job":
            job_id = str(params["job_id"])
            job = self.store.get_archive_job(job_id)
            if not job:
                raise KeyError("archive job not found")
            if job.state in {"completed", "failed", "canceled"}:
                return job.to_dict()
            control = self._archive_controls.setdefault(job_id, _TaskControl())
            control.cancel.set()
            if job.state == "queued":
                job = self.store.transition_archive_job(job_id, "canceled", expected_revision=params.get("expected_revision"))
                self._archive_event("ArchiveCanceled", job, reason="explicit_cancel")
            return self.store.get_archive_job(job_id).to_dict()
        if method == "captcha_history":
            return [activity_history.captcha_history_entry(row)
                    for row in self.store.list_captcha_history(int(params.get("limit", 200)))]
        if method == "shortlink_history":
            return activity_history.shortlink_history(
                self.store.list_recent_shortlink_chains(int(params.get("limit", 30))))
        if method == "task_activity_counts":
            captchas, hops = self.store.task_activity_counts(list(params.get("ids") or []))
            return activity_history.activity_counts(captchas, hops)
        if method == "task_timeline":
            task = self.store.get(params["id"])
            if task is None:
                raise KeyError("task not found")
            return activity_history.build_task_timeline(
                task,
                transitions=self.store.list_transitions(task.id, 500),
                challenges=self.store.list_captcha_challenges(task_id=task.id),
                chain=self.store.list_shortlink_chain(task.id),
                attempts=self.store.list_provider_attempts(task.id),
                diagnostics=self.store.list_diagnostics(task.id, 500),
            )
        if method == "captcha_list_pending":
            return self.captcha.list_pending_challenges(params.get("task_id"))
        if method == "captcha_get_challenge":
            challenge = self.captcha.get_challenge(params["challenge_id"])
            if not challenge:
                raise KeyError("captcha challenge not found")
            return challenge.to_dict()
        if method == "captcha_get_active_ui":
            return self.captcha.get_active_ui_challenge()
        if method == "captcha_solve":
            challenge_id = params["challenge_id"]
            solution = params.get("solution", {})
            solver_id = params.get("solver_id", "interactive_ui")
            challenge = self.captcha.get_challenge(challenge_id)
            if challenge is not None and params.get("generation") is not None:
                refused = challenge_lifecycle.command_outcome(challenge, int(params["generation"]))
                if refused:
                    return {"success": False, "challenge_id": challenge_id, "outcome": refused,
                            "lifecycle": challenge_lifecycle.projection(challenge)}

            if not challenge:
                # Fallback for browser_handoff tasks which don't create a formal captcha challenge
                fallback_task = self.store.get(challenge_id)
                if fallback_task and fallback_task.state == "needs_user":
                    from .captcha import CaptchaChallenge
                    f_params = dict(fallback_task.user_challenge or {})
                    if "page_url" not in f_params and "url" not in f_params:
                        f_params["page_url"] = fallback_task.source_url
                        f_params["url"] = fallback_task.source_url
                    challenge = CaptchaChallenge(id=challenge_id, task_id=fallback_task.id, params=f_params)

            # If no pre-solved payload supplied (e.g. user clicked Solve in UI), run automated solver in background
            is_manual = False
            if isinstance(solution, dict):
                is_manual = any(bool(v) for v in solution.values())
            elif solution:
                is_manual = True

            # Package challenges route through the single idempotent package solver so
            # repeated clicks reuse one Clearcote run and every member resumes together.
            if challenge is not None:
                challenge_group = (challenge.params or {}).get("group_id")
                if challenge_group:
                    challenge_host = (challenge.params or {}).get("origin_host") or ""
                    challenge_url = (challenge.params or {}).get("page_url") or (challenge.params or {}).get("url") or ""
                    # Name the challenge so this path shares a dedupe key with an
                    # armed auto-solve. Without it the two routes key differently
                    # and both launch Clearcote for the same challenge.
                    if is_manual:
                        return self._solve_package_captcha(challenge_group, challenge_host, challenge_url,
                                                          solution=solution, solver_id=solver_id,
                                                          challenge_id=challenge.id)
                    return self._solve_package_captcha(challenge_group, challenge_host, challenge_url,
                                                       challenge_id=challenge.id)

            if not is_manual:
                task = self.store.get(challenge.task_id) if challenge and challenge.task_id else None
                if task:
                    task.user_challenge = {
                        **(task.user_challenge or {}),
                        "solver_active": True,
                        "solver_engine": "Clearcote",
                    }
                    self.store.save(task)
                    self.events.emit("TaskUpdated", task.id, {"user_challenge": task.user_challenge})

                def _bg_solve_worker() -> None:
                    try:
                        from .browser_solver import solver_daemon
                        target_url = (challenge.params or {}).get("page_url") or (challenge.params or {}).get("url") if challenge else ""
                        if not target_url and task and task.source_url:
                            target_url = task.source_url
                        if not target_url:
                            logger.error("Background captcha solve worker: No target URL for task %s", challenge.task_id if challenge else "unknown")
                            return
                        cookies = (challenge.params or {}).get("cookies") if challenge else None
                        task_id = challenge.task_id if challenge else None
                        res = solver_daemon.solve_challenge_sync(target_url, 90.0, cookies, task_id)
                        if res.get("success"):
                            token_val = res.get("turnstile_token") or (res.get("cookies") or {}).get("cf_clearance") or "cleared"
                            sol = {
                                "token": token_val,
                                "turnstile_token": res.get("turnstile_token"),
                                "cf-turnstile-response": token_val,
                                "cf_clearance": (res.get("cookies") or {}).get("cf_clearance"),
                                "cookies": res.get("cookies", {}),
                                "engine": res.get("engine", "Clearcote"),
                                "direct_url": res.get("direct_url"),
                            }
                            self.captcha.solve_challenge(challenge_id, sol, "clearcote")
                            if task and task.id:
                                if res.get("direct_url"):
                                    sol["direct_url"] = res["direct_url"]
                                self._resume_after_answer(task, challenge, sol)
                        else:
                            if task and task.id:
                                cur_task = self.store.get(task.id) or task
                                cur_task.user_challenge = {
                                    **(cur_task.user_challenge or {}),
                                    "solver_active": False,
                                    "solver_error": res.get("error") or "Automated solve failed",
                                }
                                self.store.save(cur_task)
                                self.events.emit("TaskUpdated", cur_task.id, {"user_challenge": cur_task.user_challenge})
                    except Exception as exc:
                        logger.warning("Background captcha solve worker failed: %s", exc)
                        if task and task.id:
                            cur_task = self.store.get(task.id) or task
                            cur_task.user_challenge = {
                                **(cur_task.user_challenge or {}),
                                "solver_active": False,
                                "solver_error": str(exc),
                            }
                            self.store.save(cur_task)
                            self.events.emit("TaskUpdated", cur_task.id, {"user_challenge": cur_task.user_challenge})

                threading.Thread(target=_bg_solve_worker, daemon=True).start()
                return {"success": True, "challenge_id": challenge_id, "started": True, "solver_active": True}

            success = self.captcha.solve_challenge(challenge_id, solution, solver_id)
            if success and challenge and challenge.task_id:
                task = self.store.get(challenge.task_id)
                if task:
                    self._resume_after_answer(task, challenge, solution if isinstance(solution, dict) else {"token": solution})
            return {"success": success, "challenge_id": challenge_id,
                    "token_received": bool(solution_token(solution)) if success else False}
        if method == "captcha_handoff_open":
            return self.handoffs.open(params["challenge_id"], params.get("generation"), str(params.get("profile") or "default"))
        if method == "captcha_handoff_return":
            result = self.handoffs.redeem(params.get("handoff"))
            if result["outcome"] == "accepted" and result.get("task_id"):
                task = self.store.get(result["task_id"])
                challenge = self.captcha.get_challenge(result["challenge_id"])
                if task and challenge and challenge.solution:
                    self._resume_after_answer(task, challenge, challenge.solution)
            return result
        if method == "history_list":
            return self.store.list_history(int(params.get("limit", 2000)))
        if method == "history_clear":
            return {"removed": self.store.clear_history(params.get("task_ids"))}
        if method == "package_challenges":
            return self._package_challenge_summary(str(params["group_id"]))
        if method == "solve_multipart_captcha":
            group_id = params.get("group_id") or params.get("groupId") or ""
            target_host = (params.get("host") or "").lower().strip()
            page_url = params.get("page_url") or params.get("pageUrl") or ""
            return self._solve_package_captcha(group_id, target_host, page_url, solver_id="clearcote")
        if method == "captcha_skip":
            challenge_id = params["challenge_id"]
            scope = params.get("scope", "single")
            skipped = self.captcha.get_challenge(challenge_id)
            if skipped is not None and params.get("generation") is not None:
                refused = challenge_lifecycle.command_outcome(skipped, int(params["generation"]))
                if refused and refused != "duplicate":
                    return {"success": False, "challenge_id": challenge_id, "outcome": refused}
            success = self.captcha.skip_challenge(challenge_id, scope)
            self.handoffs.cancel(challenge_id)
            return {"success": success, "challenge_id": challenge_id, "scope": scope}
        if method == "captcha_report_result":
            challenge_id = params["challenge_id"]
            valid = bool(params.get("valid", True))
            reported = self.captcha.get_challenge(challenge_id)
            if reported is not None and challenge_lifecycle.state_of(reported) == challenge_lifecycle.VERIFYING:
                verifier = self.captcha.verifier
                (verifier.accept if valid else verifier.reject)(challenge_id, str(params.get("evidence") or "reported by the caller"))
            else:
                self.captcha.report_result(challenge_id, valid)
            return {"reported": True, "challenge_id": challenge_id, "valid": valid}
        if method == "captcha_get_config":
            return self.captcha.get_config()
        if method == "captcha_set_config":
            return self.captcha.set_config(params.get("config", params))
        if method == "captcha_check_balance":
            solver_id = params.get("solver_id", "twocaptcha")
            for s in self.captcha.solvers:
                if s.solver_id == solver_id and hasattr(s, "get_balance"):
                    balance = asyncio.run_coroutine_threadsafe(s.get_balance(), self._loop).result(timeout=10)
                    return {"solver_id": solver_id, "balance": balance}
            return {"solver_id": solver_id, "balance": 0.0}
        if method == "captcha_loopback_url":
            challenge_id = params["challenge_id"]
            return {"url": self.captcha.loopback_solver.get_url(challenge_id)}
        if method == "clearcote_status":
            from .clearcote_manager import get_clearcote_status
            return get_clearcote_status()
        if method == "clearcote_install":
            from .clearcote_manager import install_clearcote
            return install_clearcote()
        if method == "clearcote_install_start":
            from .clearcote_manager import install_job
            return install_job.start()
        if method == "clearcote_install_progress":
            from .clearcote_manager import install_job
            return install_job.snapshot()
        if method == "clearcote_uninstall":
            from .clearcote_manager import uninstall_clearcote
            return {"uninstalled": uninstall_clearcote()}
        if method == "archive_detect_package":
            return self.archive_orchestrator.inspect_file(params["path"])
        if method == "archive_join_parts":
            return self.archive_orchestrator.process_package(
                directory=params["directory"],
                base_name=params["base_name"],
                format_type=params.get("format_type", "split_binary"),
                output_directory=params.get("output_directory"),
                delete_parts_on_success=bool(params.get("delete_parts_on_success", False)),
                password_ref=params.get("password_ref"),
            )
        if method == "archive_pipeline":
            path = Path(params["path"]).resolve()
            inspection = self.archive_orchestrator.inspect_file(path)
            if inspection.get("is_multipart"):
                if not inspection.get("complete"):
                    return {"state": "waiting_for_parts", **inspection}
                return self.archive_orchestrator.process_package(
                    path.parent, inspection["base_name"], inspection["format_type"],
                    params.get("output_directory"), bool(params.get("delete_parts_on_success", False)),
                    params.get("password_ref"))
            if not path.is_file():
                raise FileNotFoundError(str(path))
            output = effective_directory(params.get("output_directory") or self.store.get_setting(
                "archive_directory", str(self.data_dir / "archives")), self.data_dir, create=True)
            result = self.archive_backend.extract(str(path), output, params.get("password_ref"),
                                                  params.get("policy") or self.resources.policy.to_dict())
            return {"state": "completed", "input": str(path), "output_directory": output, **result}
        if method in {"shortlink_resolve_recursive", "shortlink_resolve"}:
            future = asyncio.run_coroutine_threadsafe(
                self.shortlink_resolver.resolve_chain(
                    params["url"],
                    params.get("task_id"),
                    method=params.get("method", "GET"),
                    form_data=params.get("form_data"),
                ),
                self._loop,
            )
            final_url, hops = future.result(timeout=60)
            from dataclasses import asdict
            return {"final_url": final_url, "hops": [asdict(h) for h in hops]}
        if method == "shortlink_bypass_vip_config":
            if hasattr(self.shortlink_resolver, "bypass_vip"):
                self.shortlink_resolver.bypass_vip.configure(
                    base_url=params.get("base_url"),
                    api_key=params.get("api_key"),
                    enabled=params.get("enabled"),
                    timeout=params.get("timeout"),
                )
                return {
                    "configured": True,
                    "base_url": self.shortlink_resolver.bypass_vip.base_url,
                    "enabled": self.shortlink_resolver.bypass_vip.enabled,
                }
            return {"configured": False, "error": "bypass_vip not configured"}
        if method == "shortlink_list_supported_hosts":
            from .shortlinks import load_catalog
            cat = load_catalog()
            catalog_count = len(cat.get("entries", []))
            return {
                "catalog_hosts_count": catalog_count,
                "tier0_strategies": ["base64", "reversed_base64", "hex", "rot13", "url_in_path", "known_query_params"],
                "tier1_specialized_rules": ["adshrink", "network_loop", "ouo", "rekonise", "bstlar", "spaste"],
                "tier2_cloud_fallback": "bypass.vip",
                "tier3_antibot": "flaresolverr",
            }
        if method == "route_import_wireguard":
            return self.routes.parse_wireguard_conf(params["conf_text"])
        if method == "vpn_detect_adapters":
            from .routes import detect_system_vpn_adapters
            return {"adapters": detect_system_vpn_adapters(force_refresh=bool(params.get("force", False)))}
        if method == "flaresolverr_check_health":
            is_alive = self.flaresolverr.is_available()
            health_info = None
            if is_alive:
                future = asyncio.run_coroutine_threadsafe(self.flaresolverr.get_health(), self._loop)
                try:
                    health_info = future.result(timeout=5)
                except Exception:
                    pass
            return {"available": is_alive, "endpoint": self.flaresolverr.endpoint, "health": health_info}
        if method == "flaresolverr_configure":
            if "endpoint" in params:
                self.flaresolverr.endpoint = str(params["endpoint"]).rstrip("/")
                self.store.set_setting("flaresolverr_endpoint", self.flaresolverr.endpoint)
            return {"configured": True, "endpoint": self.flaresolverr.endpoint, "available": self.flaresolverr.is_available()}
        if method == "flaresolverr_resolve":
            future = asyncio.run_coroutine_threadsafe(
                self.flaresolverr.resolve(
                    url=params["url"],
                    method=params.get("method", "GET"),
                    post_data=params.get("post_data"),
                    max_timeout_ms=int(params.get("max_timeout_ms", 60000)),
                ),
                self._loop,
            )
            resolution = future.result(timeout=70)
            from dataclasses import asdict
            return asdict(resolution)
        if method == "crawl_page_matrix":
            url = str(params.get("url") or "").strip()
            if not url:
                raise ValueError("Missing 'url' parameter")
            force_headless = bool(params.get("force_headless", False))
            return self.crawl_page_matrix(url, force_headless=force_headless)
        if method == "audio_solver_configure":
            solver = getattr(self.captcha, "audio_solver", None)
            if solver:
                if "speech_service" in params:
                    solver.speech_service = params["speech_service"]
                if "wit_api_key" in params:
                    solver.wit_api_key = params["wit_api_key"]
                if "google_api_key" in params:
                    solver.google_api_key = params["google_api_key"]
                if "enabled" in params:
                    solver.enabled = bool(params["enabled"])
                return {"configured": True, "speech_service": solver.speech_service, "enabled": solver.enabled}
            return {"configured": False, "error": "audio solver not found"}
        if method == "route_list_active_pool":
            profiles = self.routes.profiles()
            pool_status = []
            for p in profiles:
                if p.get("enabled", True):
                    try:
                        h = self.routes.health(p["id"])
                        pool_status.append({"profile": p, "healthy": h.get("healthy", False), "public_ip": h.get("public_ip")})
                    except Exception as e:
                        pool_status.append({"profile": p, "healthy": False, "error": str(e)})
            return pool_status
        if method == "provider_list_quota_locks":
            task_id = params.get("task_id")
            attempts = self.store.list_provider_attempts(task_id) if task_id else []
            return [row for row in attempts if row.get("outcome") == "quota_exceeded"]
        if method == "resolve":
            items = self.plugins.resolve(_normalize_input_url(params["url"]), params.get("secrets"))
            return [item.to_dict() for item in items]
        if method in {"archive_probe", "archive_list", "archive_test", "archive_extract"}:
            operation = method.removeprefix("archive_")
            if operation == "probe":
                return self.archive_backend.probe(params["path"], params.get("password_ref"), params.get("policy"))
            if operation == "list":
                return self.archive_backend.list(params["path"], params.get("password_ref"), params.get("policy"))
            if operation == "test":
                return self.archive_backend.test(params["path"], params.get("password_ref"), params.get("policy"))
            task_id = params.get("task_id")
            job_id = params.get("job_id", str(uuid4()))
            policy = params.get("policy") or self.resources.policy.to_dict()
            job = ArchiveJob(job_id, task_id or "", params["path"], params["output_directory"],
                             None, params.get("password_ref"), policy, "running")
            if task_id and self.store.get(task_id):
                self.store.save_archive_job(job)
                self.events.emit("ArchiveStarted", task_id, {"job_id": job_id, "path": params["path"]})
            try:
                result = self.archive_backend.extract(params["path"], params["output_directory"],
                                                       params.get("password_ref"), policy)
                job.state = "completed"
                if task_id and self.store.get(task_id):
                    self.store.save_archive_job(job)
                    self.events.emit("ArchiveCompleted", task_id, {"job_id": job_id, "result": result})
                return {"job_id": job_id, **result}
            except Exception as exc:
                job.state, job.error = "failed", str(exc)
                if task_id and self.store.get(task_id):
                    self.store.save_archive_job(job)
                    self.events.emit("ArchiveFailed", task_id, {"job_id": job_id, "error": str(exc)})
                raise
        if method == "extract_task":
            task = self.store.get(params["task_id"])
            if not task:
                raise KeyError("task not found")
            output_root = effective_directory(params.get("output_directory") or self.store.get_setting(
                "archive_directory", str(self.data_dir / "archives")), self.data_dir, create=True)
            task_output = Path(output_root) / task.id
            task_output.mkdir(parents=True, exist_ok=True)
            allowed = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz"}
            jobs = []
            for item_index, item in enumerate(task.resolved):
                relative = item.relative_path or item.display_name
                archive_path = (Path(task.destination).resolve() / relative).resolve()
                destination_root = Path(task.destination).resolve()
                if archive_path != destination_root and destination_root not in archive_path.parents:
                    raise ValueError("archive path escapes task destination")
                if archive_path.suffix.lower() not in allowed or not archive_path.is_file():
                    continue
                job_id = str(uuid4())
                output_directory = task_output / str(item_index)
                output_directory.mkdir(parents=True, exist_ok=True)
                policy = params.get("policy") or self.resources.policy.to_dict()
                job = ArchiveJob(job_id, task.id, str(archive_path), str(output_directory),
                                 archive_path.suffix.lower().lstrip("."), task.password_ref, policy, "running")
                self.store.save_archive_job(job)
                self.events.emit("ArchiveStarted", task.id, {"job_id": job_id, "path": str(archive_path)})
                try:
                    result = self.archive_backend.extract(str(archive_path), str(output_directory),
                                                         task.password_ref, policy)
                    job.state = "completed"
                    self.store.save_archive_job(job)
                    self.events.emit("ArchiveCompleted", task.id, {"job_id": job_id, "result": result})
                    jobs.append({"job_id": job_id, "path": str(archive_path), "output_directory": str(output_directory), **result})
                except Exception as exc:
                    job.state, job.error = "failed", str(exc)
                    self.store.save_archive_job(job)
                    self.events.emit("ArchiveFailed", task.id, {"job_id": job_id, "error": str(exc)})
                    raise
            return {"task_id": task.id, "archives": jobs, "output_directory": str(task_output)}
        if method == "add_task":
            idem = params.get("idempotency_key")
            if idem:
                cached = self.store.get_idempotent(str(idem), "add_task")
                if cached is not None:
                    return cached
            input_url = _normalize_input_url(params.get("url"))
            destination = effective_directory(params.get("destination") or self.store.get_setting(
                "download_directory", str(self.data_dir / "downloads")), self.data_dir, create=True)
            route_profile_id = params.get("route_profile_id") or self.store.get_setting("active_route_profile", "direct")
            known_profiles = {item.get("id") for item in self.routes.profiles() if isinstance(item, dict) and item.get("id")}
            known_profiles.add("direct")
            if route_profile_id not in known_profiles:
                raise ValueError("unknown route profile")
            task = DownloadTask(input_url, destination, params.get("display_name"), params.get("password_ref"),
                                route_profile_id=route_profile_id,
                                alternate_urls=params.get("alternate_urls", params.get("alternatives", [])),
                                priority=int(params.get("priority", 0)), queue_id=params.get("queue_id", "default"),
                                queue_order=int(params.get("queue_order", 0)), scheduled_at=params.get("scheduled_at"),
                                category=params.get("category"), duplicate_strategy=params.get("duplicate_strategy", "skip"),
                                source_fingerprint=hashlib.sha256(input_url.encode("utf-8")).hexdigest(),
                                request_headers={str(key): str(value) for key, value in (params.get("request_headers", {}) or {}).items()
                                                 if str(key).lower() not in {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}}, referrer=params.get("referrer"),
                                browser_context=params.get("browser_context", {}),
                                selected_item_ids=list(params["selected_item_ids"]) if "selected_item_ids" in params and params["selected_item_ids"] is not None else None,
                                account_ref=params.get("account_ref") or (params.get("browser_context") or {}).get("account_ref"))
            # Score the display_name; upgrade it if the raw value is a hash/slug/missing
            _url_cands = _title_scorer.extract_url_candidates(input_url)
            _scored_name = _title_scorer.best_title(_url_cands, task.display_name or "")
            if _scored_name and _scored_name != task.display_name:
                task.display_name = _scored_name
            if not self.store.get_queue(task.queue_id):
                raise ValueError("unknown queue")
            if task.duplicate_strategy not in {"skip", "overwrite", "rename", "prompt"}:
                raise ValueError("invalid duplicate strategy")
            if params.get("folder_path"):
                task.folder_path = str(params["folder_path"])
            if params.get("size") is not None:
                try:
                    task.size = int(params["size"])
                except (ValueError, TypeError):
                    telemetry_bus.record(
                        level="DEBUG",
                        subsystem="engine:intake",
                        message="[SIZE_IGNORED] Non-numeric size hint ignored during task intake",
                        context={"task_id": task.id, "size": str(params.get("size"))[:64]},
                    )
            is_mp, pkey, pnum, part1 = self._multipart_intake_identity(task)
            critical_trace.mark(
                "multipart.intake",
                event="state",
                identity=critical_trace.TraceIdentity(
                    package_id=pkey or None, task_id=task.id,
                    part_number=pnum if is_mp else None,
                    attempt_id=task.retry_count + 1,
                ),
                multipart=is_mp,
                leader_task_id=part1.id if part1 else None,
                initial_state=task.state,
            )
            if is_mp and pkey:
                part1_pnum = (part1.package_part_number or self._task_multipart_info(part1)[2]) if part1 else 1
                if pnum > 1 and (part1 is None or part1_pnum >= pnum):
                    # Out-of-order intake: a non-leader never dispatches before Part 1.
                    if part1 is not None:
                        task.package_leader_id = part1.id
                    task.state = "pending_probe"
                    task.error = "Waiting for Part 1 to resolve"
                    if part1 and part1.size and task.size is None:
                        task.size = part1.size
                elif part1 is not None and part1_pnum < pnum:
                    task.package_leader_id = part1.id
                    if not self._part1_proven(part1):
                        task.state = "pending_probe"
                        task.error = f"Waiting for Part {part1_pnum} to resolve"
                    if part1.size and task.size is None:
                        task.size = part1.size
                if pnum == 1:
                    self._hold_package_siblings_for_leader(task, pkey)
                    if task.size:
                        self._propagate_multipart_size(task)
            self.store.save(task)
            self.events.emit("TaskCreated", task.id, task.to_dict(), f"created:{task.id}")
            result = task.to_dict()
            if idem:
                self.store.save_idempotent(str(idem), "add_task",
                                           hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest(), result)
            return result
        if method == "download_task":
            existing = self.store.get(params["id"])
            if existing and (existing.state in {"resolving", "preflight", "downloading", "verifying", "postprocessing", "retrying", "running"} or existing.state in _TERMINAL_TASK_STATES):
                return existing.to_dict()
            task = self._check_task_revision(params["id"], params.get("expected_revision", params.get("revision")))
            normalized_url = _normalize_input_url(task.source_url)
            if normalized_url != task.source_url:
                task.source_url = normalized_url
                task.source_fingerprint = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
                self.store.save(task)
            is_mp, pkey, pnum, part1 = self._multipart_intake_identity(task)
            if is_mp and pkey:
                # A solved per-file challenge owns an engine-private provider
                # continuation. Reapplying the sentinel gate here would discard
                # that progress and strand the sibling in pending_probe while
                # part 1 waits for it at the transfer-admission barrier.
                has_provider_continuation = task.id in getattr(self, "_provider_continuations", {})
                part1_pnum = (part1.package_part_number or self._task_multipart_info(part1)[2]) if part1 else 1
                if pnum > 1 and (part1 is None or part1_pnum >= pnum) and not has_provider_continuation:
                    task.package_leader_id = part1.id if part1 is not None else None
                    self._transition(task, "pending_probe", "TaskStateChanged", "Waiting for Part 1 to resolve")
                    self.store.save(task)
                    return task.to_dict()
                if part1 is not None and part1_pnum < pnum:
                    task.package_leader_id = part1.id
                    if not self._part1_proven(part1) and not has_provider_continuation:
                        self._transition(task, "pending_probe", "TaskStateChanged",
                                         f"Waiting for Part {part1_pnum} to resolve")
                        self.store.save(task)
                        return task.to_dict()
                    if has_provider_continuation and not self._part1_proven(part1):
                        self._log_task(
                            task, "debug", "multipart_admission",
                            "Resuming file-owned provider continuation past the sentinel gate",
                            {"package_key": pkey, "leader_id": part1.id,
                             "reason": "provider_continuation_ready"},
                        )
                    if part1.size and task.size is None:
                        task.size = part1.size
                    if task.state == "pending_probe":
                        task.state = "queued"
                        task.error = None
                    self.store.save(task)
            self._assert_task_can_activate(task)
            queue = self.store.get_queue(task.queue_id or "default")
            if queue and (queue.get("paused") or not queue.get("enabled")):
                task.state, task.paused_reason = "queued", "Queue is paused or disabled"
                self.store.save(task)
            if self._engine_paused:
                task.state, task.paused_reason = "queued", "Engine is paused"
                self.store.save(task)
                return task.to_dict()
            if task.scheduled_at is not None and float(task.scheduled_at) > time.time():
                task.state, task.paused_reason = "queued", "Waiting for scheduled start"
                self.store.save(task)
                return task.to_dict()
            if task.id not in self._futures or self._futures[task.id].done():
                control = _TaskControl()
                self._controls[task.id] = control
                task.state, task.error = "queued", None
                # An answer on its way to the site keeps its marker: the resumed
                # attempt's outcome is the site's verdict on it.
                carried = task.user_challenge if isinstance(task.user_challenge, dict) and task.user_challenge.get("verifying") else {}
                task.paused_reason, task.user_action = None, None
                task.user_challenge = {k: carried[k] for k in ("challenge_id", "verifying", "generation") if k in carried}
                self.store.save_with_event(task, "TaskQueued", task.to_dict(), f"queued:{task.id}:{task.retry_count}")
                backend = params.get("backend")
                if backend is not None:
                    backend = str(backend).strip().lower()
                    if backend not in {"custom", "rust"}:
                        raise ValueError("backend must be custom or rust")
                group = (urlsplit(task.source_url).hostname or "unknown").lower()
                future = asyncio.run_coroutine_threadsafe(
                    self._enqueue_task(task.id, params.get("secrets", {}), control, backend, group),
                    self._loop,
                )
                self._futures[task.id] = future
            return task.to_dict()
        if method == "pause_task":
            control = self._controls.get(params["id"])
            if control:
                control.pause.set()
            if hasattr(self, "captcha"):
                self.captcha.cancel_challenges_for_task(params["id"])
            self._stall_watchdog_state.pop(params["id"], None)
            result = self._set_state(params["id"], "paused", params.get("expected_revision", params.get("revision")))
            self.events.emit("TaskPaused", params["id"], result)
            return result
        if method == "resume_task":
            task = self._check_task_revision(params["id"], params.get("expected_revision", params.get("revision")))
            if task.state in {"canceled", "failed"}:
                return self.dispatch("retry_task", params)
            self._assert_task_can_activate(task)
            control = self._controls.get(params["id"])
            if control:
                control.pause.clear()
            if self._loop and self._loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(self.resources.reset_circuits(), self._loop).result(timeout=2)
                except Exception:
                    pass
            self.events.emit("TaskResumed", params["id"], task.to_dict())
            return self.dispatch("download_task", {"id": params["id"], "expected_revision": params.get("expected_revision", params.get("revision")), "secrets": params.get("secrets", {})})
        if method == "set_task_options":
            task = self.store.get(params["id"])
            if not task:
                raise KeyError("task not found")
            allowed = {key: params[key] for key in ("priority", "queue_id", "queue_order", "scheduled_at", "category", "duplicate_strategy") if key in params}
            if "priority" in allowed and allowed["priority"] is not None:
                try:
                    allowed["priority"] = int(allowed["priority"])
                except (ValueError, TypeError):
                    raise ValueError("priority must be an integer")
            if "queue_order" in allowed and allowed["queue_order"] is not None:
                try:
                    allowed["queue_order"] = int(allowed["queue_order"])
                except (ValueError, TypeError):
                    raise ValueError("queue_order must be an integer")
            if "scheduled_at" in allowed and allowed["scheduled_at"] is not None:
                try:
                    allowed["scheduled_at"] = float(allowed["scheduled_at"])
                except (ValueError, TypeError):
                    raise ValueError("scheduled_at must be a timestamp number")
            if "queue_id" in allowed and not self.store.get_queue(allowed["queue_id"]):
                raise ValueError("unknown queue")
            if "duplicate_strategy" in allowed and allowed["duplicate_strategy"] not in {"skip", "overwrite", "rename", "prompt"}:
                raise ValueError("invalid duplicate strategy")
            # Handle display_name separately — optionally rename file on disk for completed tasks
            if "display_name" in params:
                new_name = str(params["display_name"]).strip()
                if not new_name:
                    raise ValueError("display_name cannot be empty")
                old_dest = getattr(task, "destination", None) or getattr(task, "folder_path", None)
                if old_dest and task.state == "completed":
                    old_path = Path(old_dest)
                    if old_path.exists() and old_path.is_file():
                        new_path = old_path.with_name(new_name)
                        try:
                            old_path.rename(new_path)
                            task.destination = str(new_path)
                        except OSError as exc:
                            raise ValueError(f"could not rename file on disk: {exc}") from exc
                task.display_name = new_name
            for key, value in allowed.items():
                setattr(task, key, value)
            expected_revision = params.get("expected_revision", params.get("revision"))
            if expected_revision is None:
                self.store.save(task)
            else:
                self.store.save_if_revision(task, int(expected_revision))
            return task.to_dict()
        if method in {"retry_task", "retry_failed"}:
            if self._loop and self._loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(self.resources.reset_circuits(), self._loop).result(timeout=2)
                except Exception as err:
                    telemetry_bus.record(
                        level="WARN",
                        subsystem="engine:concurrency",
                        message="[CIRCUIT_RESET_FAILED] Could not reset transfer circuits before retry",
                        context={"error": str(err)[:200]},
                    )
            ids = [params["id"]] if method == "retry_task" else [task.id for task in self.store.list() if task.state == "failed"]
            result = []
            for task_id in ids:
                task = self._check_task_revision(task_id, params.get("expected_revision") if method == "retry_task" else None)
                # An explicit user retry overrides a stale host-pressure freeze:
                # without this the retried task would sit behind the breaker cooldown.
                retry_host = (urlsplit(task.source_url or "").hostname or "").lower()
                if retry_host:
                    concurrency_auditor.reset_breaker(retry_host, reason="manual_retry")
                task.state, task.error, task.paused_reason = "queued", None, None
                expected_revision = params.get("expected_revision") if method == "retry_task" else None
                if expected_revision is None:
                    self.store.save(task)
                else:
                    self.store.save_if_revision(task, int(expected_revision))
                retry_params = {"id": task.id, "secrets": params.get("secrets", {})}
                if params.get("backend") is not None:
                    retry_params["backend"] = params["backend"]
                result.append(self.dispatch("download_task", retry_params))
            return result[0] if method == "retry_task" and result else result
        if method == "cancel_task":
            control = self._controls.get(params["id"])
            if control:
                control.cancel.set()
            if hasattr(self, "captcha"):
                self.captcha.cancel_challenges_for_task(params["id"])
            self._stall_watchdog_state.pop(params["id"], None)
            result = self._set_state(params["id"], "canceled", params.get("expected_revision", params.get("revision")))
            self.events.emit("TaskCanceled", params["id"], result)
            return result
        if method == "delete_task":
            delete_files = bool(params.get("delete_files", False) or params.get("delete_from_disk", False))
            raw_ids = params.get("ids")
            if raw_ids and isinstance(raw_ids, list):
                target_ids = [str(x) for x in raw_ids]
            elif "id" in params and params["id"]:
                target_ids = [str(params["id"])]
            else:
                return {"affected_ids": [], "skipped": [{"id": "", "reason": "no task id specified"}]}

            deleted_ids = []
            skipped = []
            affected_all = []
            last_rev = None

            for tid in target_ids:
                self._deleted_task_ids.add(tid)
                task = self.store.get(tid)
                if not task:
                    skipped.append({"id": tid, "reason": "task not found"})
                    continue
                control = self._controls.get(tid)
                if control:
                    control.cancel.set()
                if hasattr(self, "captcha"):
                    self.captcha.cancel_challenges_for_task(tid)
                self._stall_watchdog_state.pop(tid, None)
                self._controls.pop(tid, None)
                self._telemetry_samples.pop(tid, None)
                self._last_progress_save.pop(tid, None)
                fut = self._futures.pop(tid, None)
                if fut and hasattr(fut, "cancel"):
                    try:
                        fut.cancel()
                    except Exception:
                        pass
                is_mp, deleted_pkey, _ = self._task_multipart_info(task)
                self.store.delete(tid)
                if is_mp and deleted_pkey:
                    self._prune_multipart_probe_state(deleted_pkey)
                if delete_files:
                    self._delete_task_files(task)
                self.events.emit("TaskDeleted", tid, {"id": tid})
                deleted_ids.append(tid)
                affected_all.append(tid)
                last_rev = task.revision

            return {
                "deleted": deleted_ids[0] if deleted_ids else None,
                "affected_ids": affected_all,
                "skipped": skipped,
                "revision": last_rev,
            }
        raise KeyError(f"unknown engine method: {method}")

    @staticmethod
    def _safe_unlink(path: Path) -> bool:
        """Safely unlink a file or symlink with backoff for Windows file locks."""
        if not path.is_file() and not path.is_symlink():
            return False
        for attempt in range(5):
            try:
                path.unlink(missing_ok=True)
                return True
            except (PermissionError, OSError):
                if attempt < 4:
                    time.sleep(0.08 * (attempt + 1))
                else:
                    try:
                        trash = path.with_name(f"{path.name}.del_{uuid4().hex[:6]}")
                        path.rename(trash)
                        trash.unlink(missing_ok=True)
                        return True
                    except Exception:
                        pass
        return False

    def _delete_task_files(self, task: DownloadTask) -> None:
        """Thoroughly remove downloaded, partial, and temp files from disk."""
        if not task.destination:
            return
        dest = Path(task.destination).expanduser().resolve()
        candidate_files: list[Path] = []
        candidate_dirs: list[Path] = []

        if dest.is_file():
            candidate_files.append(dest)
        elif dest.is_dir():
            candidate_dirs.append(dest)

        for item in (task.resolved or []):
            if item.relative_path:
                candidate_files.append((dest / item.relative_path).resolve())
                if item.display_name:
                    candidate_files.append((dest / item.relative_path / item.display_name).resolve())
            if item.display_name:
                candidate_files.append((dest / item.display_name).resolve())

        if task.display_name:
            candidate_files.append((dest / task.display_name).resolve())

        if task.folder_path:
            fp = Path(task.folder_path).expanduser().resolve()
            if fp.is_dir():
                candidate_dirs.append(fp)

        all_targets: set[Path] = set()
        for f in candidate_files:
            all_targets.add(f)
            parent = f.parent
            name = f.name
            all_targets.add(parent / f"{name}.part")
            all_targets.add(parent / f"{name}.crdownload")
            all_targets.add(parent / f"{name}.tmp")
            all_targets.add(parent / f"{name}.partial")
            all_targets.add(f.with_suffix(f.suffix + ".part"))
            all_targets.add(f.with_suffix(f.suffix + ".crdownload"))
            all_targets.add(f.with_suffix(f.suffix + ".tmp"))

        for target in all_targets:
            self._safe_unlink(target)

        for d in candidate_dirs:
            dl_dir = self.store.get_setting("download_directory")
            if dl_dir and d == Path(str(dl_dir)).expanduser().resolve():
                continue
            if d == self.data_dir or self.data_dir in d.parents or d in self.data_dir.parents:
                continue
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except Exception:
                pass


    def _progress(self, task: DownloadTask, count: int, item_index: int | None = None, item_bytes: int | None = None) -> None:
        if task.size and count > task.size:
            count = task.size
        now = time.time()
        task.completed_bytes = count
        task.heartbeat_at = now
        window = self._telemetry_samples.get(task.id)
        if isinstance(window, tuple):
            t0, b0 = window[0], window[1]
            window = deque([(t0, b0)], maxlen=40)
            self._telemetry_samples[task.id] = window
        elif window is None:
            window = deque(maxlen=40)
            self._telemetry_samples[task.id] = window
        window.append((now, count))
        cutoff = now - 1.5
        while len(window) > 2 and window[0][0] < cutoff:
            window.popleft()

        if len(window) >= 2:
            oldest_time, oldest_bytes = window[0]
            if count < oldest_bytes:
                window.clear()
                window.append((now, count))
            else:
                elapsed = now - oldest_time
                delta_bytes = count - oldest_bytes
                if elapsed >= 0.20:
                    current_rate = delta_bytes / elapsed
                    task.speed_bytes_per_second = max(0.0, current_rate)
                    prev_avg = task.average_speed_bytes_per_second or 0.0
                    task.average_speed_bytes_per_second = (prev_avg * 0.8) + (current_rate * 0.2) if prev_avg else current_rate
        elif not task.speed_bytes_per_second:
            task.speed_bytes_per_second = 0.0
        task.eta_seconds = ((task.size - count) / task.average_speed_bytes_per_second
                            if task.size and task.average_speed_bytes_per_second > 0 and task.size >= count else None)
        task.telemetry_updated_at = now
        sess_logger = download_loggers.get(task.id)
        if sess_logger:
            sess_logger.record_progress(
                completed_bytes=count,
                current_speed_bps=float(task.speed_bytes_per_second or 0.0),
                average_speed_bps=float(task.average_speed_bytes_per_second or 0.0),
                eta_seconds=task.eta_seconds,
            )
        task.attempt_count = max(task.attempt_count, task.retry_count + 1)
        last_saved = self._last_progress_save.get(task.id, 0.0)
        is_complete = bool(task.size and count >= task.size)
        if (now - last_saved >= 0.250) or is_complete:
            self._last_progress_save[task.id] = now
            if item_index is not None:
                item = task.resolved[item_index] if item_index < len(task.resolved) else None
                if item:
                    self.store.save_item_progress(task.id, item.item_id or f"{item_index}:{item.display_name}",
                                                  item_bytes or 0, "downloading")
                self.store.save_segment(task.id, item_index, 0, 0, item.size if item else None, False,
                                        "downloading", item_bytes or 0,
                                        (item.metadata or {}).get("validator") if item else None)
            self.store.save_progress(task)
            # W4/W5 fix: push live progress fields to the frontend via the event
            # outbox so child speed and parent progress update in real time rather
            # than only through snapshot polling. No revision bump — progress ticks
            # are informational and must not race with state transitions.
            self.events.emit("TaskUpdated", task.id, {
                "completed_bytes": task.completed_bytes,
                "size": task.size,
                "speed_bytes_per_second": task.speed_bytes_per_second,
                "average_speed_bytes_per_second": task.average_speed_bytes_per_second,
                "eta_seconds": task.eta_seconds,
            }, dedupe_key=f"progress:{task.id}")

    def _bulk_transfer_action(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Apply a global action without deleting task history or partial files."""
        action = {"tasks_pause_all": "pause", "tasks_resume_all": "resume", "tasks_stop_all": "stop"}[method]
        affected: list[str] = []
        skipped: list[dict[str, str]] = []
        active_states = {"resolving", "preflight", "downloading", "retrying"}
        for task in self.store.list():
            if action == "pause":
                if task.state in active_states:
                    control = self._controls.get(task.id)
                    if control:
                        control.pause.set()
                    previous = task.state
                    task.state, task.paused_reason = "paused", "Paused by global control"
                    self.store.save_with_event(task, "TaskPaused", task.to_dict(), from_state=previous)
                    affected.append(task.id)
                elif task.state == "queued":
                    task.paused_reason = "Waiting for global resume"
                    task.state = "paused"
                    self.store.save_with_event(task, "TaskPaused", task.to_dict(), from_state="queued")
                    affected.append(task.id)
                else:
                    skipped.append({"id": task.id, "reason": f"state {task.state} is not pausable"})
            elif action == "resume":
                if task.state == "paused":
                    control = self._controls.get(task.id)
                    if control:
                        control.pause.clear()
                    task.state, task.error, task.paused_reason = "queued", None, None
                    self.store.save_with_event(task, "TaskResumed", task.to_dict(), from_state="paused")
                    self.dispatch("download_task", {"id": task.id})
                    affected.append(task.id)
                elif task.state == "needs_user":
                    skipped.append({"id": task.id, "reason": "user action required"})
                else:
                    skipped.append({"id": task.id, "reason": f"state {task.state} is not globally resumable"})
            else:
                if task.state in active_states or task.state in {"queued", "paused", "needs_user"}:
                    control = self._controls.get(task.id)
                    if control:
                        control.cancel.set()
                    previous = task.state
                    task.state, task.error, task.paused_reason = "canceled", "Canceled by global stop", None
                    self.store.save_with_event(task, "TaskCanceled", task.to_dict(), from_state=previous)
                    affected.append(task.id)
                else:
                    skipped.append({"id": task.id, "reason": f"state {task.state} is already terminal"})
        if action == "resume":
            self._engine_paused = False
            self.store.set_setting("engine_paused", False)
        return {"action": action, "affected_ids": affected, "skipped": skipped,
                "affected_count": len(affected), "skipped_count": len(skipped)}

    def _set_state(self, task_id: str, state: str, expected_revision: int | None = None) -> dict[str, Any]:
        if task_id in self._deleted_task_ids:
            raise KeyError("task not found")
        task = self.store.get(task_id)
        if not task:
            raise KeyError("task not found")
        previous = task.state
        if previous in _TERMINAL_TASK_STATES and state == previous:
            return task.to_dict()
        if previous in _TERMINAL_TASK_STATES and state != previous:
            raise ValueError(f"terminal download state {previous} cannot transition to {state}")
        task.state = state
        if expected_revision is None:
            self.store.save(task)
        else:
            self.store.save_if_revision(task, int(expected_revision))
        self.store.record_transition(task.id, task.revision, previous, state, "TaskStateChanged")
        return task.to_dict()

    def _transition(self, task: DownloadTask, state: str, event_type: str, error: str | None = None) -> None:
        if task.id in self._deleted_task_ids:
            return
        previous = task.state
        if previous in _TERMINAL_TASK_STATES and state == previous:
            self._log_task(task, "debug", "state",
                           f"Task already in terminal state {state}; skipping duplicate {event_type}",
                           {"event": event_type})
            return
        if previous in _TERMINAL_TASK_STATES and state != previous:
            raise ValueError(f"terminal download state {previous} cannot transition to {state}")
        task.state, task.error = state, error
        if state == "downloading":
            task.started_at = task.started_at or time.time()
            task.backend = task.backend or "custom"
            task.attempt_count = max(task.attempt_count, task.retry_count + 1)
            task.integrity_state = "unverified"
        if state in {"resolving", "preflight", "downloading", "verifying", "postprocessing", "retrying"}:
            task.lease_id = task.lease_id or str(uuid4())
            task.heartbeat_at = time.time()
        elif state in {"completed", "failed", "canceled", "paused", "queued"}:
            task.lease_id, task.heartbeat_at = None, None
        task.finished_at = time.time() if state in {"completed", "failed", "canceled"} else None
        self.store.save_with_event(task, event_type, {**task.to_dict(), "reason": error}, from_state=previous)
        if state in {"completed", "failed", "canceled"}:
            self.store.record_history(task.to_dict())
        self._signal_multipart_transfer_events()
        self._log_task(task, "info" if state not in {"failed", "needs_user"} else "warn",
                       "state", f"Task state changed to {state}", {"event": event_type, "error": error})
        sync_stage = lifecycle.STATE_STAGE_SYNC.get(state)
        if sync_stage and task.stage != sync_stage:
            self._transition_stage(task, sync_stage, source=f"state:{state}")

    def _transition_stage(self, task: DownloadTask, stage: TaskStage,
                           detail: dict[str, Any] | None = None, source: str = "engine",
                           *, refresh_only: bool = False) -> None:
        """Deterministic lifecycle stage transition with microsecond telemetry.

        A stage change persists a durable ``TaskStageChanged`` event. Detail
        refreshes on the same stage (e.g. per-second countdown ticks) avoid
        revision churn by writing progress and emitting ``TaskUpdated``.
        """
        previous_stage = task.stage
        previous_entered_at = task.stage_entered_at
        entered_at = lifecycle.now_us()
        forced = False
        if previous_stage != stage:
            if not lifecycle.is_legal_transition(previous_stage, stage):
                forced = True
                self._log_task(
                    task, "warn", "lifecycle",
                    f"Illegal stage transition {previous_stage} -> {stage} rejected by table; starting new lifecycle epoch",
                    {"from_stage": previous_stage, "to_stage": stage, "source": source})
            task.stage = stage
            task.stage_entered_at = entered_at
            task.stage_detail = lifecycle._safe_detail(detail or {})
            task.stage_history = (task.stage_history or [])[-(lifecycle.MAX_STAGE_HISTORY - 1):] + [
                lifecycle.history_entry(previous_stage, stage, task.stage_detail, entered_at, forced, source)]
        else:
            task.stage_detail = {**(task.stage_detail or {}), **lifecycle._safe_detail(detail or {})}
        if refresh_only and previous_stage == stage:
            self.store.save_progress(task)
            self.events.emit("TaskUpdated", task.id, {
                "stage": task.stage, "stage_detail": task.stage_detail, "stage_entered_at": task.stage_entered_at})
            return
        held_seconds = round(entered_at - previous_entered_at, 3) if previous_entered_at and previous_stage != stage else None
        self.store.save_with_event(task, "TaskStageChanged", {
            "stage": task.stage, "previous_stage": previous_stage,
            "stage_detail": task.stage_detail, "stage_entered_at": task.stage_entered_at,
            "held_seconds": held_seconds, "forced": forced, "source": source, "reason": None,
        }, from_state=task.state)
        self._log_task(
            task, "info", "lifecycle",
            f"[{str(stage).upper()}] {lifecycle.stage_label(stage)}"
            + (f" (previous stage held {held_seconds}s)" if held_seconds is not None else ""),
            {"from_stage": previous_stage, "to_stage": task.stage, "entered_at_us": lifecycle.format_ts_us(entered_at),
             "held_seconds": held_seconds, "forced": forced, "source": source, "stage_detail": task.stage_detail})

    def _on_solver_stage_threadsafe(self, task_id: str, stage: str, data: dict[str, Any]) -> None:
        """Schedule solver stage handling onto the engine loop (never do DB I/O on the actor thread)."""
        if not task_id or self._loop is None or not self._loop.is_running():
            return
        try:
            self._loop.call_soon_threadsafe(self._on_solver_stage, task_id, stage, data)
        except Exception as err:
            telemetry_bus.record(
                level="WARN",
                subsystem="engine:captcha",
                message="[SOLVER_STAGE_DROPPED] Could not schedule solver stage onto the engine loop",
                context={"task_id": task_id, "stage": stage, "error": str(err)[:200]},
            )

    def _on_solver_stage(self, task_id: str, stage: str, data: dict[str, Any]) -> None:
        if not task_id:
            return
        task = self.store.get(task_id)
        if not task or task.state not in {"resolving", "queued", "preflight", "downloading", "needs_user", "retrying"}:
            return
        challenge_update = dict(task.user_challenge or {})
        challenge_update["solver_stage"] = stage
        if "countdown_seconds" in data:
            challenge_update["countdown_seconds"] = data["countdown_seconds"]
        if "direct_url" in data:
            challenge_update["primed"] = True
        task.user_challenge = challenge_update
        canonical = lifecycle.SOLVER_STAGE_MAP.get(stage)
        if canonical is None:
            if stage in lifecycle.SOLVER_DETAIL_STEPS and task.stage:
                # Progress INSIDE a solve. Refresh the detail so the UI can say
                # what the solver is doing, but leave the task's stage alone:
                # rewriting it per micro-step is what made a single CAPTCHA look
                # like solving -> getting metadata -> solving -> waiting timer.
                detail = {**(task.stage_detail or {}), "source": "solver", "solver_step": stage}
                if data.get("countdown_seconds") is not None:
                    detail["countdown_seconds"] = int(data["countdown_seconds"])
                self._transition_stage(task, task.stage, detail,
                                       source=f"solver:{stage}", refresh_only=True)
                self.events.emit("TaskUpdated", task.id, {"user_challenge": challenge_update})
                return
            self._log_task(task, "debug", "lifecycle",
                           f"Unmapped solver stage '{stage}' recorded without lifecycle transition",
                           {"solver_stage": stage})
            return
        detail = lifecycle.solver_stage_detail(canonical, data)
        if canonical == "hoster_wait_timer":
            countdown = data.get("countdown_seconds")
            if countdown is not None:
                detail["countdown_seconds"] = int(countdown)
            if task.stage == canonical:
                # Per-second countdown ticks refresh detail without revision churn.
                self._transition_stage(task, canonical, detail, source=f"solver:{stage}", refresh_only=True)
                self.events.emit("TaskUpdated", task.id, {"user_challenge": challenge_update})
                return
            self._transition_stage(task, canonical, detail, source=f"solver:{stage}")
            self.events.emit("TaskUpdated", task.id, {"user_challenge": challenge_update})
            return
        if canonical == "direct_link_acquired":
            first = task.resolved[0] if task.resolved else None
            if first is not None:
                detail["content_length"] = first.size
                detail["accept_ranges"] = bool(first.headers.get("Accept-Ranges") or first.headers.get("accept-ranges")) if first.headers else None
        self._transition_stage(task, canonical, detail, source=f"solver:{stage}")
        self.events.emit("TaskUpdated", task.id, {"user_challenge": challenge_update})

    def _on_captcha_coordinator(self, task_id: str | None, coordinator_state: str,
                                params: dict[str, Any]) -> None:
        """Maps durable CAPTCHA coordinator phases onto the task lifecycle stage."""
        if not task_id:
            return
        task = self.store.get(task_id)
        if not task or task.state not in {"resolving", "queued", "preflight", "downloading", "needs_user", "retrying"}:
            return
        canonical = lifecycle.COORDINATOR_STAGE_MAP.get(coordinator_state)
        if canonical is None:
            self._log_task(task, "debug", "lifecycle",
                           f"Unmapped coordinator state '{coordinator_state}' recorded without lifecycle transition",
                           {"coordinator_state": coordinator_state})
            return
        detail: dict[str, Any] = {
            "source": "captcha_coordinator",
            "coordinator_state": coordinator_state,
            "challenge_id": params.get("challenge_id") or params.get("id"),
            "solver": params.get("solver"),
        }
        if coordinator_state == "manual_required":
            detail["manual_required"] = True
            detail["manual_reason"] = params.get("manual_reason", "Clearcote did not finish the challenge")
        self._transition_stage(task, canonical, detail, source=f"captcha:{coordinator_state}")

    def _captcha_auto_solve_enabled(self) -> bool:
        """The user opted in to solving every challenge without a click."""
        try:
            return bool(self._ui_settings()["captcha"].get("captchaAutoSolve", False))
        except Exception:
            return False

    def _on_captcha_manual_required(self, challenge: Any) -> None:
        """Take the dashboard's "Solve CAPTCHA" action on the user's behalf.

        Fires once per challenge: if the automatic solve fails, the challenge
        stays parked for a manual click instead of looping.
        """
        if not self._captcha_auto_solve_enabled():
            return
        attempted = getattr(self, "_captcha_auto_solve_attempted", None)
        if attempted is None:
            attempted = self._captcha_auto_solve_attempted = set()
        if challenge.id in attempted:
            return
        attempted.add(challenge.id)
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:captcha",
            message=f"[CAPTCHA_AUTOSOLVE] Auto-solving challenge {challenge.id} (auto-solve enabled)",
            context={"challenge_id": challenge.id, "task_id": challenge.task_id},
            tier="engine",
        )

        def _run() -> None:
            try:
                self.dispatch("captcha_solve", {"challenge_id": challenge.id})
            except Exception as exc:
                telemetry_bus.record(
                    level="WARNING",
                    subsystem="engine:captcha",
                    message=(f"[CAPTCHA_AUTOSOLVE_FAILED] Auto-solve for {challenge.id} failed: {exc}; "
                             "the challenge stays parked for a manual solve"),
                    context={"challenge_id": challenge.id, "error": str(exc)},
                    tier="engine",
                )

        threading.Thread(target=_run, daemon=True, name="captcha-auto-solve").start()

    def _log_task(self, task: DownloadTask, level: str, stage: str, message: str,
                  details: dict[str, Any] | None = None) -> None:
        safe_details = _redact_diagnostic(details or {})
        safe_message = _redact_diagnostic(message)
        self.store.record_diagnostic(task.id, level, stage, str(safe_message), safe_details)
        self.events.emit("Diagnostic", task.id, {"level": level, "stage": stage,
                                                  "message": str(safe_message), "details": safe_details})
        lvl_map = {"debug": "DEBUG", "info": "INFO", "warn": "WARN", "warning": "WARN", "error": "ERROR", "fatal": "FATAL"}
        telemetry_bus.record(
            level=lvl_map.get(level.lower(), "INFO"),
            subsystem="engine:transfer",
            message=f"[{task.id[:8]}] ({stage}) {safe_message}",
            context={"task_id": task.id, "display_name": task.display_name, "provider": task.provider, "stage": stage, **safe_details},
        )


    def _on_scheduler_error(self, exc: BaseException) -> None:
        """Record a worker exception that would otherwise vanish into a completion future."""
        try:
            telemetry_bus.record(
                level="ERROR",
                subsystem="engine:transfer",
                message=f"[SCHEDULER_WORKER_ERROR] {_redact_diagnostic(str(exc))}",
                context={"error": _redact_diagnostic(str(exc)), "type": type(exc).__name__},
            )
        except Exception:
            pass

    @staticmethod
    def _diagnostic_recommendations(task: DownloadTask) -> list[str]:
        recommendations: list[str] = []
        if task.state == "needs_user":
            recommendations.append("Complete the requested browser, password, login, or CAPTCHA action, then resume the task.")
        if task.provider == "generic" and task.size and task.completed_bytes == 0:
            recommendations.append("Inspect the shortlink chain and response type; an HTML result should be browser-assisted rather than downloaded.")
        if task.state == "failed":
            recommendations.append("Review the latest error log and retry after correcting the provider or destination issue.")
        return recommendations

    @staticmethod
    def _task_multipart_info(task: DownloadTask) -> tuple[bool, str, int]:
        """Detects if a task belongs to a multipart package and returns (is_multipart, pkg_key, part_number)."""
        name = task.display_name or (urlsplit(task.source_url or "").path.split("/")[-1] if task.source_url else "")
        info = MultiPartDetector.detect(name) if name else None
        if not (info and info.is_multipart) and task.source_url:
            url_leaf = urlsplit(task.source_url).path.split("/")[-1]
            if url_leaf and url_leaf != name:
                url_info = MultiPartDetector.detect(url_leaf)
                if url_info and url_info.is_multipart:
                    info = url_info
                    name = url_leaf

        if info and info.is_multipart:
            host = (urlsplit(task.source_url or "").hostname or "").lower()
            if task.package_key and not (os.path.isabs(task.package_key) or ":\\" in task.package_key):
                key = task.package_key
            elif task.folder_path and Path(task.folder_path).name.lower() == info.base_name.lower():
                key = task.folder_path
            else:
                key = f"{host}:{info.base_name.lower()}" if host else info.base_name.lower()
            return True, key, info.part_number
        if task.package_key and not (os.path.isabs(task.package_key) or ":\\" in task.package_key):
            m = re.search(r"[._-]part(\d+)", name, re.IGNORECASE) if name else None
            pnum = int(m.group(1)) if m else (task.package_part_number or 1)
            return True, task.package_key, pnum
        if task.folder_path and name:
            m = re.search(r"[._-]part(\d+)", name, re.IGNORECASE)
            if m:
                clean_base = re.sub(r"[._-]part\d+.*$", "", name, flags=re.IGNORECASE)
                if Path(task.folder_path).name.lower() == clean_base.lower():
                    key = task.folder_path
                else:
                    host = (urlsplit(task.source_url or "").hostname or "").lower()
                    key = f"{host}:{clean_base.lower()}" if host else clean_base.lower()
                return True, key, int(m.group(1))
        return False, "", 1

    @staticmethod
    def _package_key_base(pkey: str) -> str:
        """Return the comparison base name of a canonical, folder, or legacy package key."""
        if not pkey:
            return ""
        if ":" in pkey and not re.match(r"^[A-Za-z]:[\\/]", pkey):
            return pkey.split(":", 1)[1].lower()
        return (Path(pkey).name or pkey).lower()

    def _multipart_intake_identity(self, task: DownloadTask) -> tuple[bool, str, int, DownloadTask | None]:
        """Canonicalize a new task's package identity and locate its current Part 1 leader.

        Legacy absolute-path package keys are treated as read-only: they are never
        rewritten here, only interpreted, so older persisted tasks stay intact.
        """
        is_mp, pkey, pnum = self._task_multipart_info(task)
        if not is_mp or not pkey:
            return False, "", 1, None
        if not task.package_key or os.path.isabs(task.package_key) or ":\\" in task.package_key:
            task.package_key = pkey
        task.package_part_number = pnum
        pkg_tasks = [
            t for t in self.store.list()
            if t.id != task.id and (t.package_key == pkey or self._task_multipart_info(t)[1] == pkey)
        ]
        part1 = next(
            (t for t in pkg_tasks if (t.package_part_number == 1 or self._task_multipart_info(t)[2] == 1)),
            None,
        )
        if part1 is None and pnum > 1 and pkg_tasks:
            candidate = min(pkg_tasks, key=lambda t: (t.package_part_number or self._task_multipart_info(t)[2] or 999))
            candidate_pnum = candidate.package_part_number or self._task_multipart_info(candidate)[2]
            if candidate_pnum < pnum:
                part1 = candidate
        return True, pkey, pnum, part1

    def _hold_package_siblings_for_leader(self, leader: DownloadTask, pkey: str) -> None:
        """Place every non-leader member of the package in pending_probe."""
        leader_host = (urlsplit(leader.source_url or "").hostname or "").lower()
        base = self._package_key_base(pkey)
        for sibling in self.store.list():
            if sibling.id == leader.id:
                continue
            s_mp, s_pkey, s_pnum = self._task_multipart_info(sibling)
            if not s_mp or s_pnum <= 1:
                continue
            s_host = (urlsplit(sibling.source_url or "").hostname or "").lower()
            same_host = not leader_host or not s_host or leader_host == s_host
            if not same_host:
                continue
            if s_pkey != pkey and sibling.package_key != pkey and self._package_key_base(s_pkey) != base:
                continue
            changed = False
            if sibling.package_leader_id != leader.id:
                sibling.package_leader_id = leader.id
                changed = True
            if sibling.state == "queued":
                sibling.state = "pending_probe"
                sibling.error = "Waiting for Part 1 to resolve"
                changed = True
            if changed:
                self.store.save(sibling)
                if sibling.state == "pending_probe":
                    self.events.emit("TaskStateChanged", sibling.id,
                                     {"state": "pending_probe", "error": sibling.error})
                else:
                    self.events.emit("TaskUpdated", sibling.id, {"package_leader_id": leader.id})

    def _package_members(self, pkey: str, host: str = "") -> list[DownloadTask]:
        """All stored members of a package, tolerating legacy folder-path keys."""
        if not pkey:
            return []
        base = self._package_key_base(pkey)
        host = (host or "").lower().strip()
        members: list[DownloadTask] = []
        for task in self.store.list():
            is_mp, task_pkey, _ = self._task_multipart_info(task)
            if not is_mp or not task_pkey:
                continue
            task_host = (urlsplit(task.source_url or "").hostname or "").lower()
            if task.package_key == pkey or task_pkey == pkey:
                members.append(task)
                continue
            if base and self._package_key_base(task_pkey) == base and (not host or not task_host or task_host == host):
                members.append(task)
        return members

    def _signal_multipart_transfer_events(self) -> None:
        """Wake preflight admission checks after any durable task transition."""
        events = getattr(self, "_multipart_transfer_events", None)
        loop = getattr(self, "_loop", None)
        if not events or loop is None or loop.is_closed():
            return
        pending_events = tuple(events.values())

        def signal() -> None:
            for event in pending_events:
                event.set()

        loop.call_soon_threadsafe(signal)

    @staticmethod
    def _requires_atomic_package_preflight(task: DownloadTask) -> bool:
        host = (urlsplit(task.source_url or "").hostname or "").lower()
        if host == "datanodes.to" or host.endswith(".datanodes.to"):
            return True
        return any((item.provider or "").lower() == "datanodes" for item in task.resolved)

    async def _await_multipart_transfer_admission(
        self,
        task: DownloadTask,
        package_key: str,
        host: str,
    ) -> None:
        """Hold DataNodes payload until every live sibling owns a direct plan.

        DataNodes permits CAPTCHA work in parallel but can reject a sibling's
        final step-two POST after payload consumption begins. Preflight is the
        durable boundary proving that a direct plan exists, so all live package
        members rendezvous here before any backend receives a URL.
        """
        if not package_key or not self._requires_atomic_package_preflight(task):
            return
        event = self._multipart_transfer_events.setdefault(package_key, asyncio.Event())
        ready_states = {"preflight", "downloading", "verifying", "postprocessing", "completed"}
        abandoned_states = {"failed", "canceled", "paused"}
        # A sibling still held behind the sentinel is waiting for THIS task to
        # advance, so it can never be waited ON: part 1 reaches preflight and
        # waits for part 2, while part 2 is only released once part 1 is
        # downloading. That circular wait deadlocked the package with every task
        # stranded in preflight and no error anywhere.
        deferred_states = {"pending_probe", "queued"}
        deadline = time.monotonic() + _MULTIPART_ADMISSION_TIMEOUT_SECONDS
        logged_wait = False
        while True:
            members = self._package_members(package_key, host)
            live = [member for member in members if member.state not in abandoned_states]
            pending = [member for member in live
                       if member.state not in ready_states and member.state not in deferred_states]
            if not pending:
                self._log_task(task, "info", "multipart_admission",
                               "Package direct plans are ready; releasing payload transfer",
                               {"package_key": package_key, "members": len(members),
                                "abandoned": [member.id for member in members
                                              if member.state in abandoned_states]})
                critical_trace.mark(
                    "multipart.transfer_barrier.released",
                    event="state",
                    identity=critical_trace.TraceIdentity(
                        package_id=package_key,
                        task_id=task.id,
                        part_number=task.package_part_number,
                        attempt_id=task.retry_count + 1,
                    ),
                    members=len(members),
                    abandoned=sum(1 for member in members if member.state in abandoned_states),
                )
                return
            if not logged_wait:
                logged_wait = True
                self._log_task(task, "info", "multipart_admission",
                               "Holding payload until sibling direct plans are ready",
                               {"package_key": package_key,
                                "pending": [{"id": member.id, "state": member.state}
                                            for member in pending]})
                critical_trace.mark(
                    "multipart.transfer_barrier.waiting",
                    event="state",
                    identity=critical_trace.TraceIdentity(
                        package_id=package_key,
                        task_id=task.id,
                        part_number=task.package_part_number,
                        attempt_id=task.retry_count + 1,
                    ),
                    pending=[member.id for member in pending],
                )
            event.clear()
            # Close the clear/check race: a sibling may have transitioned
            # immediately before the event was reset.
            refreshed = self._package_members(package_key, host)
            if all(member.state in ready_states | abandoned_states | deferred_states
                   for member in refreshed):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Never wait forever. A barrier whose failure mode is "the
                # download silently never starts" is worse than one that
                # occasionally admits a payload early, so this is loud and bounded.
                self._log_task(task, "warning", "multipart_admission",
                               f"[MULTIPART_ADMISSION_TIMEOUT] Releasing payload after "
                               f"{_MULTIPART_ADMISSION_TIMEOUT_SECONDS:.0f}s; siblings never reached a "
                               f"direct plan",
                               {"package_key": package_key,
                                "pending": [{"id": member.id, "state": member.state}
                                            for member in pending]})
                return
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                continue

    def _resolve_package_reference(self, package_ref: str, host: str = "") -> tuple[str, list[DownloadTask]]:
        """Accept canonical keys, legacy folder paths, and bare package names from older UI builds."""
        ref = (package_ref or "").strip()
        host = (host or "").lower().strip()
        if not ref:
            return "", []
        ref_base = self._package_key_base(ref) or (Path(ref).name or ref).lower()
        ref_host_prefix = ""
        if ":" in ref and not re.match(r"^[A-Za-z]:[\\/]", ref):
            ref_host_prefix = ref.split(":", 1)[0].lower()
        canonical = ""
        members: list[DownloadTask] = []
        for task in self.store.list():
            is_mp, pkey, _ = self._task_multipart_info(task)
            if not is_mp or not pkey:
                continue
            task_host = (urlsplit(task.source_url or "").hostname or "").lower()
            base_match = self._package_key_base(pkey) == ref_base and (
                not host or not task_host or task_host == host)
            if base_match and ref_host_prefix and ":" in pkey:
                base_match = pkey.split(":", 1)[0].lower() == ref_host_prefix
            matched = (
                pkey == ref
                or task.package_key == ref
                or (task.folder_path and os.path.normcase(os.path.normpath(task.folder_path)) == os.path.normcase(os.path.normpath(ref)))
                or base_match
            )
            if not matched:
                continue
            members.append(task)
            if not canonical or (os.path.isabs(canonical) or ":\\" in canonical) and not (os.path.isabs(pkey) or ":\\" in pkey):
                canonical = pkey
        if canonical and (os.path.isabs(canonical) or ":\\" in canonical) and host:
            base = self._package_key_base(canonical)
            if base:
                canonical = f"{host}:{base}"
        if not canonical and host and ref_base:
            canonical = f"{host}:{ref_base}"
        return canonical, members

    @staticmethod
    def _package_host_hint(members: list[DownloadTask]) -> str:
        for member in members:
            host = (urlsplit(member.source_url or "").hostname or "").lower()
            if host:
                return host
        return ""

    @staticmethod
    def _package_page_url(members: list[DownloadTask], pkey: str) -> str:
        for member in members:
            challenge = member.user_challenge or {}
            url = challenge.get("page_url") or challenge.get("url")
            if url:
                return str(url)
            if member.source_url:
                return member.source_url
        return ""

    def _package_leader_id(self, task: DownloadTask | None, pkey: str, host: str = "") -> str | None:
        if task is not None and task.package_leader_id:
            return task.package_leader_id
        members = self._package_members(pkey, host)
        part1 = next(
            (t for t in members if t.package_part_number == 1 or self._task_multipart_info(t)[2] == 1),
            None,
        )
        if part1 is not None:
            return part1.id
        return task.id if task is not None else None

    def _find_package_challenge(self, pkey: str, host: str = "") -> CaptchaChallenge | None:
        """One durable challenge per (package_key, host); reuse while still pending/solving."""
        if not pkey or not hasattr(self, "captcha"):
            return None
        host = (host or "").lower().strip()
        try:
            pending = self.captcha.list_pending_challenges()
        except Exception as exc:
            logger.debug("Package challenge lookup failed for %s: %s", pkey, exc)
            return None
        for row in pending:
            params = row.get("params") or {}
            if params.get("group_id") != pkey:
                continue
            if host and params.get("origin_host") and params.get("origin_host") != host:
                continue
            challenge = self.captcha.get_challenge(row.get("id"))
            if challenge is not None:
                return challenge
        return None

    def _package_challenge_is_reusable(self, existing: CaptchaChallenge, task: DownloadTask,
                                       pkey: str) -> bool:
        """Whether a sibling may adopt the package's existing challenge.

        What a package genuinely shares is CLEARANCE (host cookies), not the
        per-file token. Hosts differ:

        * clearance-sharing hosts -- part 1 solves, siblings then resolve without
          challenging at all, so one challenge covers the package naturally.
        * per-file-token hosts (DataNodes) -- every part raises its own challenge
          even with valid clearance.

        Handing every sibling the same challenge object made the second kind
        indistinguishable from the first: all five parts deduped onto one
        challenge id, so exactly one solve ran while two of three solver lanes
        sat idle and the parts queued behind it.

        A challenge is only still "the same work" while nothing is solving it.
        Once a solve is in flight its token belongs to the file that requested
        it, so a sibling needs its own.
        """
        if existing.task_id == task.id:
            return True
        inflight = getattr(self, "_package_solve_inflight", None) or set()
        if f"{pkey}:{existing.id}" in inflight or pkey in inflight:
            telemetry_bus.record(
                level="DEBUG",
                subsystem="engine:captcha",
                message=(
                    f"[PACKAGE_CHALLENGE_FORKED] {task.display_name or task.id[:8]} needs its own "
                    f"challenge: package challenge {existing.id[:8]} is already being solved for "
                    f"another part"
                ),
                context={"task_id": task.id, "package_key": pkey, "existing_challenge": existing.id},
                tier="engine",
            )
            return False
        return True

    def _ensure_package_challenge(self, task: DownloadTask, pkey: str, host: str,
                                  c_params: dict[str, Any], c_type: Any, c_timeout: float,
                                  challenge_id: str) -> tuple[CaptchaChallenge, bool]:
        existing = self._find_package_challenge(pkey, host)
        leader_id = self._package_leader_id(task, pkey, host)
        if existing is not None and self._package_challenge_is_reusable(existing, task, pkey):
            existing.params = {
                **(existing.params or {}),
                "group_id": pkey,
                "origin_host": host,
                "leader_task_id": (existing.params or {}).get("leader_task_id") or leader_id,
            }
            self.captcha.register_challenge(existing)
            return existing, False
        challenge = CaptchaChallenge(
            id=challenge_id,
            task_id=task.id,
            provider_id=task.provider or "generic",
            captcha_type=c_type,
            params={**c_params, "group_id": pkey, "origin_host": host, "leader_task_id": leader_id},
            timeout_seconds=c_timeout,
        )
        self.captcha.register_challenge(challenge)
        return challenge, True

    def _prune_multipart_probe_state(self, pkey: str | None = None,
                                     members: list[DownloadTask] | None = None) -> None:
        """Drop stagger bookkeeping once a package has no non-terminal members left."""
        keys = [pkey] if pkey else list(set(self._last_multipart_probe) | set(self._multipart_probe_delays))
        for key in keys:
            if not key:
                continue
            if members is not None and key == pkey:
                package_members = members
            else:
                package_members = self._package_members(key)
            if package_members and any(t.state not in _TERMINAL_TASK_STATES for t in package_members):
                continue
            self._last_multipart_probe.pop(key, None)
            self._multipart_probe_delays.pop(key, None)

    @staticmethod
    def _multipart_probe_stagger(has_active_transfer: bool) -> float:
        """Single calibration point for staggered sibling probe spacing (PIPE-04)."""
        base = _MULTIPART_PROBE_STAGGER_SECONDS
        if not has_active_transfer:
            base *= _MULTIPART_PROBE_STAGGER_COLD_MULTIPLIER
        jitter = base * _MULTIPART_PROBE_STAGGER_JITTER_RATIO
        return base + random.uniform(-jitter, jitter)

    def _part1_proven(self, part1: DownloadTask) -> bool:
        """Whether the sentinel part has proven the package is worth committing to.

        The sentinel exists so a dead package cannot burn a CAPTCHA per part. It
        used to wait for part 1 to reach `downloading`, but that is ~53s in:
        41s solving plus 12s resolving. Siblings then spent their own ~53s of
        pre-work while part 1 downloaded for ~51s, so each one became
        download-ready at the exact moment the previous finished. That is why a
        multipart package crawled along one part at a time with a host ceiling of
        4 and only one stream ever open -- not a cap, just a pipeline whose two
        stages are the same length.

        Clearing the challenge is the real proof: it establishes the host serves
        us and the clearance works, which is everything a sibling needs. Starting
        sibling pre-work there overlaps it with part 1's transfer instead of
        queueing behind it.
        """
        if part1.state in _MULTIPART_PART1_RELEASED_STATES:
            return True
        if (
            part1.state == "preflight"
            and critical_trace.active()
            and bool(part1.resolved)
        ):
            # The resolution benchmark deliberately holds transfer admission after
            # real preflight. Persisted resolved items intentionally redact signed
            # URLs, so the completed resolution plan is the durable proof used to
            # release sibling lanes; no fake backend or state is created.
            return True
        if part1.state == "needs_user":
            challenge = part1.user_challenge or {}
            is_multipart, package_key, _ = self._task_multipart_info(part1)
            return bool(
                is_multipart
                and package_key in self._armed_packages()
                and challenge.get("solver_active")
            )
        challenge = part1.user_challenge or {}
        if challenge and not challenge.get("solved") and challenge.get("solver_active"):
            # Still mid-solve: the path is not proven yet.
            return False
        if not challenge:
            # A host-level clearance may belong to another file/session.  It is
            # not proof that this sentinel has reached its own challenge.
            return False
        host = (urlsplit(part1.source_url or "").hostname or "").lower()
        if not host:
            return False
        try:
            from .http_client import clearance_cache
            return clearance_cache.get_clearance(host) is not None
        except Exception as exc:
            logger.debug("Clearance lookup failed for %s: %s", host, exc)
            return False

    def _armed_packages(self) -> set[str]:
        """Packages the user has authorised solving (lazily created).

        Accessed through a helper because the engine is also constructed without
        ``__init__`` in tests, matching how ``_package_solve_inflight`` is handled.
        """
        armed = getattr(self, "_package_autosolve_armed", None)
        if armed is None:
            armed = set()
            self._package_autosolve_armed = armed
        return armed

    def _autosolve_armed_package(self, pkey: str, host: str, page_url: str,
                                 *, challenge_id: str | None = None) -> None:
        """Solve a sibling challenge on a package the user already authorised.

        Runs off the caller's thread because a solve takes ~40s. The challenge id
        is passed through so siblings are deduped individually: hosts that issue
        a Turnstile per file raise one challenge per part, and they are solved
        concurrently across the solver daemon's 3 lanes (spaced 1.2s apart)
        rather than collapsing into a single run.
        """

        def _run() -> None:
            try:
                self._solve_package_captcha(pkey, host, page_url, challenge_id=challenge_id)
            except Exception as exc:
                # Never silent: the user is not watching for a click any more, so
                # a failed auto-solve must say so or the package looks hung.
                telemetry_bus.record(
                    level="WARNING",
                    subsystem="engine:captcha",
                    message=(
                        f"[PACKAGE_AUTOSOLVE_FAILED] Armed auto-solve for package {pkey} failed: {exc}; "
                        f"the challenge stays parked for a manual solve"
                    ),
                    context={"package_key": pkey, "host": host, "error": str(exc)},
                    tier="engine",
                )

        telemetry_bus.record(
            level="INFO",
            subsystem="engine:captcha",
            message=f"[PACKAGE_AUTOSOLVE_ARMED] Auto-solving sibling challenge for package {pkey} (no further clicks needed)",
            context={"package_key": pkey, "host": host},
            tier="engine",
        )
        threading.Thread(target=_run, daemon=True).start()

    def _solve_package_captcha(self, package_ref: str, host: str = "", page_url: str = "",
                               *, solution: dict[str, Any] | str | None = None,
                               solver_id: str = "clearcote",
                               challenge_id: str | None = None) -> dict[str, Any]:
        """Idempotent package-level solve shared by captcha_solve and solve_multipart_captcha.

        Accepts canonical package keys, legacy folder paths, and bare package names.
        Repeated clicks cannot spawn parallel Clearcote runs for the same package.
        """
        pkey, members = self._resolve_package_reference(package_ref, host)
        if not members:
            # UI host hints are provider ids (e.g. "datanodes"), not origin hosts.
            pkey, members = self._resolve_package_reference(package_ref, "")
        if not members:
            fallback_base = self._package_key_base(package_ref)
            if fallback_base and fallback_base != (package_ref or "").strip().lower():
                pkey, members = self._resolve_package_reference(fallback_base, "")
        page_host = (urlsplit(page_url).hostname or "").lower() if page_url else ""
        host = page_host or self._package_host_hint(members) or (host or "").lower().strip()
        canonical = pkey or (
            f"{host}:{self._package_key_base(package_ref)}"
            if host and self._package_key_base(package_ref) else (package_ref or "").strip()
        )
        if not canonical and not members:
            return {"success": False, "started": False, "group_id": package_ref,
                    "error": "unknown multipart package"}
        leader_id = self._package_leader_id(members[0] if members else None, canonical, host)
        page_url = page_url or self._package_page_url(members, canonical)
        # Solving this package once authorises solving it again: every later
        # sibling challenge on the same package solves itself instead of parking.
        if canonical:
            self._armed_packages().add(canonical)
        if solution is not None:
            applied = self._apply_package_captcha_clearance(
                canonical, host, solution, solver_id, members=members, leader_id=leader_id)
            return {"success": applied, "started": False, "manual": True, "group_id": canonical}
        inflight = getattr(self, "_package_solve_inflight", None)
        if inflight is None:
            inflight = set()
            self._package_solve_inflight = inflight
        lock = getattr(self, "_package_solve_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._package_solve_lock = lock
        # A user-triggered package solve may omit the challenge id while the
        # armed-package path supplies it. Resolve the omission before keying so
        # those two routes cannot race into duplicate browser lanes.
        if not challenge_id:
            pending_challenge = self._find_package_challenge(canonical, host)
            if pending_challenge is not None:
                challenge_id = pending_challenge.id
        # Dedupe per CHALLENGE, not per package. Repeated clicks on the same
        # challenge must collapse into one Clearcote run, but hosts that issue a
        # Turnstile per file raise a distinct challenge for every sibling, and
        # keying on the package alone silently dropped all but the first --
        # leaving the rest parked forever. Distinct challenges proceed in
        # parallel and are spaced by the solver daemon's own lane semaphore
        # (3 lanes) and 1.2s launch stagger.
        solve_key = f"{canonical}:{challenge_id}" if challenge_id else canonical
        with lock:
            if solve_key in inflight:
                telemetry_bus.record(
                    level="INFO",
                    subsystem="engine:captcha",
                    message=f"[PACKAGE_CAPTCHA_SOLVE_DEDUPED] package={canonical} host={host} challenge={challenge_id or '-'}",
                    context={"pkey": canonical, "host": host, "challenge_id": challenge_id},
                    tier="engine",
                )
                return {"success": True, "started": False, "deduped": True, "group_id": canonical}
            inflight.add(solve_key)
        for member in members:
            if member.state != "needs_user" and not member.user_challenge:
                continue
            member.user_challenge = {
                **(member.user_challenge or {}),
                "solver_active": True,
                "solver_engine": "Clearcote",
            }
            self.store.save(member)
            self.events.emit("TaskUpdated", member.id, {"user_challenge": member.user_challenge})

        def _worker() -> None:
            challenge: CaptchaChallenge | None = None
            try:
                from .browser_solver import solver_daemon
                # An armed sibling names its own challenge; only fall back to the
                # package-wide lookup when no specific one was given.
                challenge = None
                if challenge_id:
                    challenge = self.captcha.get_challenge(challenge_id)
                if challenge is None:
                    challenge = self._find_package_challenge(canonical, host)
                if challenge is not None:
                    challenge.params = {
                        **(challenge.params or {}),
                        "group_id": canonical,
                        "origin_host": host,
                        "leader_task_id": (challenge.params or {}).get("leader_task_id") or leader_id,
                        "coordinator_state": "clearcote_active",
                        "solver": solver_id,
                    }
                    self.captcha.register_challenge(challenge)
                cookies = (challenge.params or {}).get("cookies") if challenge else None
                # Attribute the solve to the part that raised the challenge; a
                # package leader fallback covers challenge-less invocations.
                solve_task_id = (challenge.task_id if challenge is not None else "") or leader_id
                solve_task = next((member for member in members if member.id == solve_task_id), None)
                if solve_task is None and solve_task_id:
                    stored_solve_task = self.store.get(solve_task_id)
                    solve_task = stored_solve_task if isinstance(stored_solve_task, DownloadTask) else None
                solve_part = self._task_multipart_info(solve_task)[2] if solve_task else None
                with critical_trace.bind(
                    package_id=canonical,
                    task_id=solve_task_id or None,
                    part_number=solve_part,
                    attempt_id=(solve_task.retry_count + 1) if solve_task else None,
                ):
                    completion_mode = (
                        "token"
                        if challenge is not None
                        and bool((challenge.params or {}).get("provider_resume"))
                        else "direct_or_token_wait"
                    )
                    if completion_mode == "token":
                        res = solver_daemon.solve_challenge_sync(
                            page_url, 90.0, cookies, solve_task_id,
                            completion_mode="token",
                        )
                    else:
                        # Preserve the established four-argument solver contract
                        # for all providers that do not advertise continuation.
                        res = solver_daemon.solve_challenge_sync(
                            page_url, 90.0, cookies, solve_task_id,
                        )
                if not res.get("success"):
                    reason = str(res.get("error") or "Automated solve failed")
                    self._fail_package_captcha(canonical, host, reason,
                                               challenge=challenge, members=members, leader_id=leader_id)
                    return
                harvested = res.get("cookies") if isinstance(res.get("cookies"), dict) else {}
                token_val = res.get("turnstile_token") or harvested.get("cf_clearance") or "cleared"
                sol: dict[str, Any] = {
                    "token": token_val,
                    "turnstile_token": res.get("turnstile_token"),
                    "cf-turnstile-response": token_val,
                    "cf_clearance": harvested.get("cf_clearance"),
                    "cookies": harvested,
                    "user_agent": res.get("user_agent") or "",
                    "engine": res.get("engine", "Clearcote"),
                }
                if res.get("direct_url"):
                    sol["direct_url"] = res["direct_url"]
                self._apply_package_captcha_clearance(
                    canonical, host, sol, "clearcote",
                    members=members, leader_id=leader_id, challenge=challenge)
            except Exception as exc:
                logger.warning("Package CAPTCHA solve worker failed for %s: %s", canonical, exc)
                self._fail_package_captcha(canonical, host, str(exc),
                                           challenge=challenge, members=members, leader_id=leader_id)
            finally:
                with lock:
                    inflight.discard(solve_key)

        threading.Thread(target=_worker, daemon=True, name="MultipartSolverWorker").start()
        return {"success": True, "started": True, "group_id": canonical}

    def _apply_package_captcha_clearance(self, pkey: str, host: str, solution: dict[str, Any] | str,
                                         solver_id: str, *, members: list[DownloadTask] | None = None,
                                         leader_id: str | None = None,
                                         challenge: CaptchaChallenge | None = None) -> bool:
        """Persist clearance, clear the shared banner, and resume only this package's members."""
        solution_dict = solution if isinstance(solution, dict) else {"token": solution}
        challenge = challenge or self._find_package_challenge(pkey, host)
        solved = False
        if challenge is not None:
            solved = self.captcha.solve_challenge(challenge.id, solution_dict, solver_id)
        # Nothing is shared yet: clearance reaches siblings (same route only) when
        # the site accepts this answer (_share_verified_clearance, MULTI-02).
        member_snapshots = members if members is not None else self._package_members(pkey, host)
        fresh_members: list[DownloadTask] = []
        for snapshot in member_snapshots:
            stored = self.store.get(snapshot.id)
            fresh_members.append(stored if isinstance(stored, DownloadTask) else snapshot)
        challenge_owner_id = challenge.task_id if challenge is not None else None
        resumed: list[str] = []
        for member in fresh_members:
            member_challenge_id = str((member.user_challenge or {}).get("challenge_id") or "")
            solved_challenge_id = str(challenge.id if challenge is not None else "")
            if challenge is None or (member_challenge_id and member_challenge_id == solved_challenge_id):
                member.user_challenge = {}
                member.user_action = None
                self.store.save(member)
                self.events.emit("TaskUpdated", member.id, {"user_challenge": {}})
            # Challenge response tokens are file/session bound. Apply a solved
            # challenge only to its owner; armed siblings will raise and solve
            # their own challenge. Always re-read the task because concurrent
            # solver workers may hold stale snapshots from before preflight.
            if member.state != "needs_user":
                continue
            if challenge_owner_id and member.id != challenge_owner_id:
                continue
            # The answer is this member's alone (token, form state, direct URL).
            resumed.append(member.id)
            self._resume_after_answer(member, challenge, solution_dict)
        if leader_id is None:
            leader_id = resumed[0] if resumed else (fresh_members[0].id if fresh_members else None)
        self.events.emit("CaptchaChallengeResolved", leader_id, {
            "groupId": pkey,
            "group_id": pkey,
            "challenge_id": challenge.id if challenge else None,
            "host": host,
            "resolved": True,
            "solver": solver_id,
            "solved": bool(solved),
            "resumed_task_ids": resumed,
        }, dedupe_key=f"package_captcha_resolved:{pkey}")
        return True

    def _package_challenge_summary(self, pkey: str) -> dict[str, Any]:
        """One view of a package's challenges (MULTI-01), built from each member's
        own challenge, generation and state; nothing is merged or shared."""
        members = self._package_members(pkey)
        rows = []
        for member in members:
            challenge_id = str((member.user_challenge or {}).get("challenge_id") or "")
            challenge = self.captcha.get_challenge(challenge_id) if challenge_id else None
            if challenge is None:
                continue
            rows.append({"task_id": member.id, "part": self._task_multipart_info(member)[2],
                         **challenge_lifecycle.projection(challenge)})
        waiting_on_user = [r for r in rows if r["next_action"] == "answer"]
        return {"group_id": pkey, "members": len(members), "challenged": rows,
                "needs_you": len(waiting_on_user),
                "next_action": "answer" if waiting_on_user else ("wait" if rows else "none")}

    def _fail_package_captcha(self, pkey: str, host: str, reason: str, *,
                              challenge: CaptchaChallenge | None = None,
                              members: list[DownloadTask] | None = None,
                              leader_id: str | None = None) -> None:
        challenge = challenge or self._find_package_challenge(pkey, host)
        if challenge is not None:
            challenge.error = reason
            challenge.params = {
                **(challenge.params or {}),
                "group_id": pkey,
                "origin_host": host,
                "leader_task_id": (challenge.params or {}).get("leader_task_id") or leader_id,
                "coordinator_state": "manual_required",
                "manual_reason": reason,
                "manual_solver_active": False,
            }
            self.captcha.register_challenge(challenge)
        fresh_members = members if members is not None else self._package_members(pkey, host)
        for member in fresh_members:
            if member.state != "needs_user":
                continue
            member.user_challenge = {
                **(member.user_challenge or {}),
                "solver_active": False,
                "solver_error": reason,
            }
            self.store.save(member)
            self.events.emit("TaskUpdated", member.id, {"user_challenge": member.user_challenge})
        if leader_id is None:
            leader_id = fresh_members[0].id if fresh_members else None
        self.events.emit("CaptchaChallengeFailed", leader_id, {
            "groupId": pkey,
            "group_id": pkey,
            "challenge_id": challenge.id if challenge else None,
            "host": host,
            "resolved": False,
            "reason": reason,
        }, dedupe_key=f"package_captcha_failed:{pkey}")

    def _propagate_multipart_size(self, task: DownloadTask) -> None:
        """Propagates discovered file size and folder path of a multipart part to all siblings."""
        is_mp, pkey, _ = self._task_multipart_info(task)
        if not is_mp or not pkey:
            return
        for sibling in self.store.list():
            if sibling.id == task.id:
                continue
            s_mp, s_pkey, s_pnum = self._task_multipart_info(sibling)
            if s_mp and (s_pkey == pkey or sibling.package_key == pkey):
                changed = False
                if task.size and task.size > 0 and (sibling.size is None or sibling.size == 0):
                    sibling.size = task.size
                    changed = True
                if task.folder_path and not sibling.folder_path:
                    sibling.folder_path = task.folder_path
                    changed = True
                if not sibling.package_key or os.path.isabs(sibling.package_key) or ":\\" in sibling.package_key:
                    sibling.package_key = pkey
                    changed = True
                if not sibling.package_part_number:
                    sibling.package_part_number = s_pnum
                    changed = True
                if changed:
                    self.store.save(sibling)
                    self.events.emit("TaskUpdated", sibling.id, {
                        "size": sibling.size,
                        "package_key": pkey,
                        "package_part_number": s_pnum,
                        "folder_path": sibling.folder_path,
                    })

    @staticmethod
    def _queue_in_window(queue: dict[str, Any]) -> bool:
        start, end = queue.get("start_hour"), queue.get("end_hour")
        if start is None or end is None:
            return True
        hour = time.localtime().tm_hour
        return start <= hour < end if start <= end else hour >= start or hour < end

    def _multipart_package_map(self, tasks: list[DownloadTask]) -> dict[str, list[DownloadTask]]:
        """Bucket multipart tasks by canonical package key, merging leader-linked strays."""
        pkg_map: dict[str, list[DownloadTask]] = {}
        for t in tasks:
            is_mp, pkey, _ = self._task_multipart_info(t)
            if is_mp and pkey:
                pkg_map.setdefault(pkey, []).append(t)

        # Merge packages that share a leader_id to prevent any split buckets
        leader_to_key: dict[str, str] = {}
        for pkey, ptasks in pkg_map.items():
            for t in ptasks:
                if t.package_part_number == 1 or self._task_multipart_info(t)[2] == 1:
                    leader_to_key[t.id] = pkey
        for t in tasks:
            if t.package_leader_id and t.package_leader_id in leader_to_key:
                correct_pkey = leader_to_key[t.package_leader_id]
                is_mp, cur_pkey, _ = self._task_multipart_info(t)
                if cur_pkey != correct_pkey:
                    if cur_pkey in pkg_map and t in pkg_map[cur_pkey]:
                        pkg_map[cur_pkey].remove(t)
                        if not pkg_map[cur_pkey]:
                            del pkg_map[cur_pkey]
                    pkg_map.setdefault(correct_pkey, []).append(t)
                    # Legacy absolute-path keys are accepted read-only: bucket them
                    # under the canonical key without rewriting persisted state.
                    if not (os.path.isabs(t.package_key or "") or ":\\" in (t.package_key or "")):
                        t.package_key = correct_pkey
                        self.store.save(t)
        return pkg_map

    def _release_multipart_probe_lanes(self, pkg_map: dict[str, list[DownloadTask]], now: float) -> None:
        """Release sibling probe lanes only after Part 1 has left resolution, with stagger.

        Part 1 must not be challenged (needs_user) or still resolving before any
        sibling is admitted, otherwise siblings create their own challenges and
        the user sees multiple prompts for one package.
        """
        for pkey, ptasks in pkg_map.items():
            part1 = next((t for t in ptasks if self._task_multipart_info(t)[2] == 1), None)
            if not part1 and ptasks:
                part1 = min(ptasks, key=lambda t: self._task_multipart_info(t)[2])
            if not part1:
                continue
            if all(t.state in _TERMINAL_TASK_STATES for t in ptasks):
                self._prune_multipart_probe_state(pkey, members=ptasks)
                continue
            part1_pnum = self._task_multipart_info(part1)[2]
            other_parts = sorted(
                [t for t in ptasks if self._task_multipart_info(t)[2] > part1_pnum],
                key=lambda t: self._task_multipart_info(t)[2]
            )
            # Parallel pre-resolution: up to 3 concurrent lanes (or fewer if total parts < 3)
            max_resolve_lanes = min(3, len(ptasks))
            active_resolving = sum(1 for t in ptasks if t.state in {"resolving", "preflight"})
            part1_released = self._part1_proven(part1)
            if not part1_released:
                for other in other_parts:
                    if other.state == "queued":
                        _, _, other_pnum = self._task_multipart_info(other)
                        critical_trace.mark(
                            "multipart.sibling_held",
                            event="state",
                            identity=critical_trace.TraceIdentity(
                                package_id=pkey, task_id=other.id, part_number=other_pnum,
                                attempt_id=other.retry_count + 1,
                            ),
                            leader_task_id=part1.id,
                            leader_state=part1.state,
                        )
                        self._transition(other, "pending_probe", "TaskStateChanged",
                                         f"Waiting for Part {part1_pnum} to start")
                        self.store.save(other)
                continue
            last_probe = self._last_multipart_probe.get(pkey, 0.0)
            delay = self._multipart_probe_delays.get(pkey)
            has_active_transfer = any(
                t.state in {"downloading", "verifying", "postprocessing", "completed"} for t in ptasks)
            if delay is None:
                delay = self._multipart_probe_stagger(has_active_transfer)
                self._multipart_probe_delays[pkey] = delay
            if active_resolving < max_resolve_lanes and (now - last_probe >= delay):
                next_pending = next((t for t in other_parts if t.state == "pending_probe"), None)
                if next_pending:
                    next_pnum = self._task_multipart_info(next_pending)[2]
                    self._last_multipart_probe[pkey] = now
                    self._multipart_probe_delays[pkey] = self._multipart_probe_stagger(has_active_transfer)
                    if part1.size and (next_pending.size is None or next_pending.size == 0):
                        next_pending.size = part1.size
                    self._transition(next_pending, "queued", "TaskQueued",
                                     f"Released for parallel pre-resolution (lane {active_resolving + 1}/{max_resolve_lanes})")
                    self.store.save(next_pending)
                    critical_trace.mark(
                        "multipart.sibling_released",
                        event="state",
                        identity=critical_trace.TraceIdentity(
                            package_id=pkey, task_id=next_pending.id, part_number=next_pnum,
                            attempt_id=next_pending.retry_count + 1,
                        ),
                        leader_task_id=part1.id,
                        leader_state=part1.state,
                        active_lanes=active_resolving + 1,
                        max_lanes=max_resolve_lanes,
                        stagger_seconds=round(delay, 6),
                    )
                    telemetry_bus.record(
                        level="INFO",
                        subsystem="engine:multipart",
                        message=(
                            f"[PACKAGE_PROBE_RELEASED] package={pkey} part={next_pnum} "
                            f"reason=part1_{part1.state} active_lanes={active_resolving + 1}/{max_resolve_lanes}"
                        ),
                        context={
                            "pkey": pkey,
                            "part": next_pnum,
                            "reason": f"part1_{part1.state}",
                            "active_lanes": active_resolving + 1,
                            "max_lanes": max_resolve_lanes,
                            "stagger_seconds": round(delay, 3),
                        },
                        tier="engine",
                    )
                    if next_pending.size:
                        self.events.emit("TaskUpdated", next_pending.id, {"size": next_pending.size})

    async def _pump(self) -> None:
        last_watchdog_check = time.time()
        last_outbox_prune = time.time()
        while True:
            try:
                now = time.time()
                # Periodic event-outbox retention: acknowledged events older than
                # 24 h (or above the row cap) and orphaned rows are pruned so the
                # outbox cannot grow unbounded over long sessions.
                if now - last_outbox_prune >= 600.0:
                    last_outbox_prune = now
                    try:
                        self.store.prune_event_outbox()
                    except Exception as exc:
                        telemetry_bus.record(
                            level="WARN",
                            subsystem="engine:db",
                            message=f"[OUTBOX_PRUNE_FAILED] {exc}",
                            context={"error": str(exc)},
                        )
                auto_start = bool(self._ui_settings()["general"].get("autoStart", True))
                tasks = self.store.list()
                if not self._engine_paused and auto_start:
                    # PIPE-01 & PIPE-04: Sentinel Part 1 Probe & Staggered Jitter Admission
                    pkg_map = self._multipart_package_map(tasks)
                    self._release_multipart_probe_lanes(pkg_map, now)
                    for task in tasks:
                        queue = self.store.get_queue(task.queue_id or "default")
                        if task.state != "queued":
                            continue
                        if self._scheduler and self._scheduler.has_job(f"task:{task.id}"):
                            continue
                        if task.id in self._futures and not self._futures[task.id].done():
                            continue
                        if not queue or not queue.get("enabled") or queue.get("paused") or not self._queue_in_window(queue):
                            continue
                        if task.scheduled_at is not None and float(task.scheduled_at) > time.time():
                            continue
                        host = (urlsplit(task.source_url or "").hostname or "").lower()
                        if host:
                            in_cd, remain, cd_reason = concurrency_auditor.is_host_in_cooldown(host)
                            # Loopback fixture/services are local resources and
                            # must never inherit a persisted internet-host pressure
                            # cooldown from an earlier test or app run.
                            if in_cd and host not in {"localhost", "127.0.0.1", "::1"}:
                                last_warn = self._host_cooldown_warned.get(host, 0.0)
                                if now - last_warn >= 30.0:
                                    self._host_cooldown_warned[host] = now
                                    telemetry_bus.record(
                                        level="WARNING",
                                        subsystem="engine:queue",
                                        message=f"Deferring task {task.id[:8]} dispatch: {host} in cooldown ({int(remain)}s remaining)",
                                        context={"host": host, "task_id": task.id, "remaining_seconds": round(remain, 1), "reason": cd_reason},
                                    )
                                continue
                            # Host-level resolve concurrency limit: allow up to 3 concurrent
                            # resolutions on the same host (coordinated with BrowserSolverDaemon's
                            # 3-lane semaphore and 1.2s launch stagger).
                            # If a challenge actually requires manual user intervention (needs_user),
                            # halt further dispatches on that host until the challenge is cleared.
                            needs_user_tasks = [
                                candidate for candidate in tasks
                                if candidate.id != task.id
                                and candidate.state == "needs_user"
                                and (urlsplit(candidate.source_url or "").hostname or "").lower() == host
                            ]
                            if needs_user_tasks:
                                task_is_mp, task_package, _ = self._task_multipart_info(task)
                                authorized_package_solve = bool(
                                    task_is_mp
                                    and task_package in self._armed_packages()
                                    and all(
                                        self._task_multipart_info(blocker)[1] == task_package
                                        and bool((blocker.user_challenge or {}).get("solver_active"))
                                        for blocker in needs_user_tasks
                                    )
                                )
                                # A solved host is not a blocked host. Once clearance
                                # is cached, siblings resolve without challenging, so
                                # holding the whole host hostage to one parked task is
                                # what made a multipart package run strictly one part
                                # at a time: every sibling challenged, so some task was
                                # always `needs_user`, so nothing else could dispatch.
                                from .http_client import clearance_cache
                                cleared = clearance_cache.get_clearance(host) is not None
                                if not cleared and not authorized_package_solve:
                                    last_warn = self._host_cooldown_warned.get(f"needs_user:{host}", 0.0)
                                    if now - last_warn >= 30.0:
                                        self._host_cooldown_warned[f"needs_user:{host}"] = now
                                        telemetry_bus.record(
                                            level="INFO",
                                            subsystem="engine:queue",
                                            message=f"Deferring task {task.id[:8]} dispatch: {host} has a pending user challenge",
                                            context={"host": host, "task_id": task.id, "reason": "needs_user_on_host"},
                                        )
                                    continue
                                if authorized_package_solve:
                                    telemetry_bus.record(
                                        level="DEBUG",
                                        subsystem="engine:queue",
                                        message=f"Dispatching {task.id[:8]} beside an authorized package solve",
                                        context={
                                            "host": host,
                                            "task_id": task.id,
                                            "package_key": task_package,
                                            "reason": "authorized_package_solver_active",
                                        },
                                    )
                                telemetry_bus.record(
                                    level="DEBUG",
                                    subsystem="engine:queue",
                                    message=(
                                        f"Dispatching task {task.id[:8]} despite a parked challenge on {host}: "
                                        f"cached clearance should resolve it without a new challenge"
                                    ),
                                    context={"host": host, "task_id": task.id, "reason": "host_clearance_cached"},
                                )

                            resolving_on_host = sum(
                                1 for t in tasks
                                if t.id != task.id
                                and t.state in {"resolving", "preflight"}
                                and (urlsplit(t.source_url or "").hostname or "").lower() == host
                            )
                            if resolving_on_host >= 3:
                                last_warn = self._host_cooldown_warned.get(f"resolve:{host}", 0.0)
                                if now - last_warn >= 30.0:
                                    self._host_cooldown_warned[f"resolve:{host}"] = now
                                    telemetry_bus.record(
                                        level="DEBUG",
                                        subsystem="engine:queue",
                                        message=f"Deferring task {task.id[:8]} dispatch: {host} resolve lanes busy ({resolving_on_host})",
                                        context={"host": host, "task_id": task.id, "reason": "resolve_lane_busy",
                                                 "resolving_on_host": resolving_on_host},
                                    )
                                continue
                        self.dispatch("download_task", {"id": task.id})

                # Startup & Stall Watchdog (checks every 2s, triggers warnings at >= 15s)
                if now - last_watchdog_check >= 2.0:
                    last_watchdog_check = now
                    # Open an upward concurrency probe on any storage host that is
                    # saturated with work waiting behind it. Without this the
                    # verified ceiling can only ever fall, never rise.
                    # Per-host stream table, once per tick. Without it a run can
                    # only be reconstructed from per-download logs after the fact,
                    # and questions like "were three streams really transferring?"
                    # cannot be answered live. Cheap: one record per active host.
                    for shost, shost_streams in list(self.storage_concurrency._active_streams.items()):
                        if not shost_streams:
                            continue
                        sample = self.storage_concurrency.sample_host_goodput(shost, now=now)
                        rows = [
                            {"stream": row["stream"],
                             "bytes": row["bytes"],
                             "interval_bytes": row["interval_bytes"],
                             "interval_seconds": round(float(row["interval_seconds"]), 6),
                             "mb_s": round(float(row["speed_bps"]) / (1024 * 1024), 2)}
                            for row in sample["streams"]
                        ]
                        productive = int(sample["productive"])
                        aggregate_mb = round(float(sample["aggregate_speed_bps"]) / (1024 * 1024), 2)
                        telemetry_bus.record(
                            level="DEBUG",
                            subsystem="engine:concurrency",
                            message=(
                                f"[HOST_STREAM_TABLE] {shost}: {len(rows)} open, {productive} productive, "
                                f"{aggregate_mb} MB/s aggregate"
                            ),
                            context={"host": shost, "open": len(rows), "productive": productive,
                                     "aggregate_mb_s": aggregate_mb,
                                     "limit": self.storage_concurrency.get_limit(shost),
                                     "streams": rows},
                            tier="engine",
                        )
                    # A finished package with no archive job is always a bug; sweep
                    # for it rather than trusting one completion event to fire.
                    try:
                        await self._reconcile_stranded_archives()
                    except Exception as exc:
                        telemetry_bus.record(
                            level="WARNING",
                            subsystem="engine:archive",
                            message=f"[ARCHIVE_RECONCILE_FAILED] Stranded-archive sweep failed: {exc}",
                            context={"error": str(exc)[:300]},
                        )
                    try:
                        self.storage_concurrency.reconsider_limits()
                    except Exception as exc:
                        telemetry_bus.record(
                            level="WARNING",
                            subsystem="engine:concurrency",
                            message=f"[STORAGE_PROBE_FAILED] Concurrency probe pass failed: {exc}",
                            context={"error": str(exc)[:300]},
                        )
                    # PIPE-06: restart streams that are genuinely dead, at most once each.
                    #
                    # This deliberately does NOT touch the host's ceiling. A slow
                    # lane is a normal DataNodes serving state, and treating it as
                    # proof the host only serves one connection is what collapsed
                    # multipart packages to a single stream: the ceiling was
                    # demoted to the productive count and then locked, so every
                    # later part waited its turn for no reason. Genuine refusal to
                    # serve arrives as 429/403 and is handled by the breaker.
                    for shost in list(self.storage_concurrency._active_streams.keys()):
                        stalled_ids = self.storage_concurrency.check_zero_byte_stalls(shost)
                        for stalled_id in stalled_ids:
                            target_task_id = stalled_id.split(":")[0]
                            stalled_task = self.store.get(target_task_id)
                            if stalled_task and stalled_task.state == "downloading":
                                productive_c = self.storage_concurrency.productive_stream_count(shost)
                                telemetry_bus.record(
                                    level="WARN",
                                    subsystem="engine:storage_concurrency",
                                    message=(
                                        f"[STORAGE_HOST_STALL_DROPPED] {shost} stream made no progress "
                                        f"(<0.5MB/s sustained past the grace period while {productive_c} "
                                        f"sibling(s) transferred); restarting it once, host ceiling unchanged"
                                    ),
                                    context={"task_id": target_task_id, "stream_id": stalled_id,
                                             "storage_host": shost, "productive_streams": productive_c},
                                    tier="engine",
                                )
                                if target_task_id in self._controls:
                                    self._controls[target_task_id].cancel.set()
                                    self._controls.pop(target_task_id, None)
                                self._futures.pop(target_task_id, None)
                                stalled_task.state = "queued"
                                stalled_task.error = f"{shost} stream made no progress; queued for one retry"
                                self.store.save(stalled_task)
                                self._transition(stalled_task, "queued", "TaskQueued", "Restarted dead stream")
                                self.storage_concurrency.unregister_stream(shost, stalled_id)
                    for task in tasks:
                        if task.state in {"queued", "resolving", "preflight", "downloading", "retrying"}:
                            st_info = self._stall_watchdog_state.get(task.id)
                            if not st_info or st_info[0] != task.state:
                                self._stall_watchdog_state[task.id] = (task.state, now, 0.0)
                            else:
                                last_st, enter_time, last_warn = st_info
                                elapsed = now - enter_time
                                if elapsed >= 15.0:
                                    is_stalled = False
                                    reason = ""
                                    if task.state in {"queued", "resolving", "preflight", "retrying"}:
                                        is_stalled = True
                                        reason = task.error or task.paused_reason or f"Waiting in {task.state} state"
                                    elif task.state == "downloading" and (task.completed_bytes or 0) == 0 and (task.speed_bytes_per_second or 0) == 0:
                                        is_stalled = True
                                        reason = "Downloading with 0 bytes transferred (waiting for host stream)"

                                    if is_stalled and (now - last_warn >= 30.0 or last_warn == 0.0):
                                        self._stall_watchdog_state[task.id] = (last_st, enter_time, now)
                                        telemetry_bus.record(
                                            level="WARN",
                                            subsystem="engine:transfer",
                                            message=f"[STALL DETECTED] Task '{task.display_name or task.source_url}' has not started transferring after {int(elapsed)}s. State: {task.state}. Reason: {reason}",
                                            context={
                                                "task_id": task.id,
                                                "url": task.source_url,
                                                "state": task.state,
                                                "elapsed_seconds": int(elapsed),
                                                "provider": task.provider,
                                                "error": task.error,
                                            },
                                        )
                        elif task.id in self._stall_watchdog_state:
                            del self._stall_watchdog_state[task.id]

                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)


    async def _run_task_async(self, task_id: str, secrets: dict[str, str], control: _TaskControl,
                              backend_name: str | None) -> None:
        if task_id in self._deleted_task_ids:
            return
        task = self.store.get(task_id)
        if not task or task.id in self._deleted_task_ids:
            return
        leases: list[str] = []
        attempt_provider = task.provider or self.plugins.detect_provider(task.source_url) or "generic"
        attempt_fingerprint = task.source_fingerprint or hashlib.sha256(task.source_url.encode("utf-8")).hexdigest()
        requested_backend = backend_name
        group = (urlsplit(task.source_url).hostname or "unknown").lower()
        metrics = self.transport_metrics.begin(task.id, requested_backend)
        task.attempt_count = max(task.attempt_count, task.retry_count + 1)
        task.telemetry_updated_at = time.time()
        self.store.save(task)
        if task.selected_item_ids is not None:
            secrets = {**secrets, "selected_item_ids": task.selected_item_ids}
        # Resolve account credentials if attached to task or available for provider/host
        acc_ref = task.account_ref or (task.browser_context or {}).get("account_ref")
        if not acc_ref:
            host_lower = (urlsplit(task.source_url).hostname or "").lower()
            prov_cand = "google-drive" if any(h in host_lower for h in ("drive.google.com", "docs.google.com")) else (task.provider or "")
            if prov_cand:
                matched_acc = self.dispatch("select_account", {"provider_id": prov_cand})
                if matched_acc:
                    acc_ref = matched_acc.get("credential_ref")
                    task.account_ref = matched_acc.get("id")
        if acc_ref:
            try:
                resolved_cred = self.secrets.resolve_operation(acc_ref)
                if resolved_cred:
                    if resolved_cred.startswith("{") and resolved_cred.endswith("}"):
                        try:
                            cred_dict = json.loads(resolved_cred)
                            if isinstance(cred_dict, dict):
                                secrets = {**secrets, **cred_dict}
                        except Exception:
                            secrets = {**secrets, "api_token": resolved_cred}
                    elif "=" in resolved_cred and ";" in resolved_cred:
                        secrets = {**secrets, "cookie": resolved_cred}
                    else:
                        secrets = {**secrets, "api_token": resolved_cred, "access_token": resolved_cred}
            except Exception as acc_err:
                self._log_task(task, "debug", "account", f"Account credential resolution notice: {acc_err}")
        try:
            route_profile = self._usable_route_profile(task.route_profile_id)
            route_http.bind_task_route(route_http.proxy_url_for(route_profile))
            self.routes.record_attempt(task.id, route_profile["id"], "selected", "task route selected")
            self._log_task(task, "info", "route", "Selected route profile", {"profile_id": route_profile["id"]})
            await self.resources.reset_circuits()
            self._transition(task, "resolving", "TaskResolving")
            self._log_task(task, "info", "resolve", "Resolving source and intermediate links", {"max_hops": 8})
            def record_hop(hop: int, url: str, provider: str) -> None:
                nonlocal attempt_provider
                if provider:
                    attempt_provider = provider
                self.store.save_shortlink_hop(task.id, hop, url, "visited", provider)
                self._log_task(task, "info", "shortlink", f"Visited resolution hop {hop}",
                               {"hop": hop, "provider": provider, "url": url})

            url_to_resolve = task.source_url
            skip_shortlink = bool(task.size and task.size > 0) or bool(task.resolved and any(item.direct_url for item in task.resolved))
            if not skip_shortlink:
                try:
                    def on_resolver_hop(h: Any) -> None:
                        self.store.save_shortlink_hop(task.id, h.hop_number, h.output_url, h.status, h.strategy)
                        self._log_task(task, "info", "shortlink", f"Bypassed shortlink hop {h.hop_number} via {h.strategy}",
                                       {"hop": h.hop_number, "strategy": h.strategy, "input_url": h.input_url, "output_url": h.output_url})

                    bypassed_target, resolver_hops = await self.shortlink_resolver.resolve_chain(
                        task.source_url, task.id, max_hops=8, progress_callback=on_resolver_hop
                    )
                    if bypassed_target and bypassed_target != task.source_url:
                        url_to_resolve = bypassed_target
                        task.source_url = bypassed_target
                        task.source_fingerprint = hashlib.sha256(bypassed_target.encode("utf-8")).hexdigest()
                        self.store.save(task)
                        self._log_task(task, "info", "shortlink", f"Shortlink bypassed to target URL: {url_to_resolve}")
                except Exception as exc:
                    self._log_task(task, "warn", "shortlink", f"Shortlink pre-resolution notice: {exc}")

            media_raw = None
            if urlsplit(url_to_resolve).path.lower().split("?", 1)[0].endswith((".m3u8", ".mpd")):
                try:
                    media_raw = await asyncio.to_thread(self.plugins.media_metadata, url_to_resolve)
                    if media_raw.get("variants") and not media_raw.get("segments"):
                        selected = media_raw.get("selected_variant") or media_raw["variants"][0]
                        variant_url = selected.get("url")
                        if variant_url:
                            import urllib.request
                            with route_http.urlopen(urllib.request.Request(variant_url), timeout=20) as response:
                                variant_body = response.read(8 * 1024 * 1024).decode("utf-8", errors="replace")
                            media_raw = parse_media(variant_url, variant_body).to_dict()
                except Exception as exc:
                    self._log_task(task, "debug", "media", f"Media planning fallback: {exc}")
                    media_raw = None
            if media_raw:
                media_name = task.display_name or Path(urlsplit(url_to_resolve).path).name or "media"
                if not Path(media_name).suffix:
                    media_name += ".mp4"
                task.resolved = [ResolvedItem("media", task.source_url, media_name,
                                              direct_url=None, metadata={"media_plan": media_raw,
                                                                         "content_type": "video/mp4"})]
            else:
                resolution_context = ResolutionContext(
                    source_url=url_to_resolve,
                    browser_session_ref=(task.browser_context or {}).get("session_ref"),
                    account_ref=(task.browser_context or {}).get("account_ref"),
                    refresh_policy="on_expiry",
                )
                target_host = (urlsplit(url_to_resolve).hostname or "").lower()
                cached_session = self.get_host_session(target_host, task.route_profile_id) if target_host else {}
                effective_secrets = {**cached_session, **secrets, "task_id": task.id}
                continuation = self._provider_continuations.get(task.id)
                if continuation:
                    expires_ns = int(continuation.get("expires_unix_ns") or 0)
                    if expires_ns > time.time_ns():
                        effective_secrets["_provider_continuation"] = continuation
                        critical_trace.mark(
                            "resolve.provider_continuation.injected", event="state",
                            resource="provider", resource_id=target_host,
                        )
                    else:
                        self._provider_continuations.pop(task.id, None)
                        telemetry_bus.record(
                            level="INFO", subsystem="engine:resolve",
                            message="[PROVIDER_CONTINUATION_EXPIRED] Suspended provider operation expired before resume",
                            context={"task_id": task.id, "host": target_host}, tier="engine",
                        )
                is_mp_resolve, mp_pkey_resolve, mp_part_resolve = self._task_multipart_info(task)
                trace_identity = {
                    "package_id": mp_pkey_resolve if is_mp_resolve else None,
                    "task_id": task.id,
                    "part_number": mp_part_resolve if is_mp_resolve else None,
                    "attempt_id": task.retry_count + 1,
                }
                if is_mp_resolve and mp_pkey_resolve:
                    # Providers must not run their own inline browser solve for
                    # package members: the package challenge coordinator owns the
                    # single solve and shares clearance with every part.
                    effective_secrets.update({
                        "multipart_package": mp_pkey_resolve,
                        "multipart_part": mp_part_resolve,
                    })

                if task.resolved and any(item.direct_url for item in task.resolved):
                    self._log_task(task, "info", "resolve", "Using pre-resolved direct download link (bypassing resolution)")
                else:
                    # Stagger start of link resolutions on the same host domain to eliminate HTTP 429 anti-bot triggers
                    if target_host:
                        if target_host not in self._domain_resolve_locks:
                            self._domain_resolve_locks[target_host] = asyncio.Lock()
                        domain_lock = self._domain_resolve_locks[target_host]
                        with critical_trace.bind(**trace_identity):
                            critical_trace.mark(
                                "resolve.host_gate.queued", event="state",
                                resource="resolve_host", resource_id=target_host,
                            )
                            async with domain_lock:
                                with critical_trace.span(
                                    "resolve.host_gate", resource="resolve_host",
                                    resource_id=target_host,
                                ):
                                    last_ts = self._domain_last_resolved.get(target_host, 0.0)
                                    now = time.time()
                                    stagger_delay = self.resolve_stagger.delay(target_host)
                                    elapsed = now - last_ts
                                    if elapsed < stagger_delay and last_ts > 0:
                                        critical_trace.mark(
                                            "resolve.host_stagger", event="state",
                                            resource="resolve_host", resource_id=target_host,
                                            wait_seconds=stagger_delay - elapsed,
                                        )
                                        await asyncio.sleep(stagger_delay - elapsed)
                                    self._domain_last_resolved[target_host] = time.time()

                    from .provider_wait import resolution_waits
                    generation = str(uuid4())
                    wait_active = [True]
                    def on_wait(data):
                        def apply_wait():
                            current = self.store.get(task_id)
                            if not wait_active[0] or control.cancel.is_set() or control.pause.is_set() or not current:
                                return
                            if current.state not in {'resolving', 'retrying', 'preflight'}:
                                return
                            waiting = data['wait_state'] == 'waiting'
                            stage = 'hoster_wait_timer' if waiting else 'resolving_metadata'
                            self._transition_stage(current, stage, data, source='provider:wait',
                                                   refresh_only=current.stage == stage)
                        self._loop.call_soon_threadsafe(apply_wait)
                    try:
                        with critical_trace.bind(**trace_identity):
                            with critical_trace.span(
                                "resolve.provider", resource="provider",
                                resource_id=target_host or attempt_provider,
                                source_fingerprint=task.source_fingerprint,
                            ):
                                with resolution_waits(task_id, generation, control, on_wait):
                                    task.resolved = await asyncio.to_thread(
                                        self.resolution_broker.resolve, resolution_context,
                                        lambda source: self.plugins.resolve_chain(source, effective_secrets, 8, record_hop))
                                # Getting files out of the site is its acceptance of any answer carried here.
                                self._settle_task_challenge(task, True, "the site served the download")
                                critical_trace.mark(
                                    "resolve.direct_urls", event="state",
                                    resolved_items=len(task.resolved),
                                    direct_items=sum(1 for item in task.resolved if item.direct_url),
                                )
                                next_delay = self.resolve_stagger.observe_success(target_host)
                                critical_trace.mark(
                                    "resolve.host_stagger.feedback", event="state",
                                    resource="resolve_host", resource_id=target_host,
                                    outcome="success", next_delay_seconds=next_delay,
                                )
                                self._provider_continuations.pop(task.id, None)
                    finally:
                        wait_active[0] = False
            for item in task.resolved:
                item.metadata = {**item.metadata, "duplicate_strategy": task.duplicate_strategy}
                item.headers = {**task.request_headers, **item.headers}
                if task.referrer:
                    item.headers.setdefault("Referer", task.referrer)
                if cached_session.get("user_agent"):
                    item.headers.setdefault("User-Agent", cached_session["user_agent"])
                if cached_session.get("cookies"):
                    for ck, cv in cached_session["cookies"].items():
                        item.cookies.setdefault(ck, cv)
            # No name yet (or only the link): name the task from what the provider found.
            if not task.display_name or "://" in task.display_name:
                task.display_name = _title_scorer.name_from_items(task.resolved) or task.display_name
            if len(task.resolved) == 1 and task.display_name:
                task.resolved[0].display_name = task.display_name
            task.provider = task.resolved[0].provider if task.resolved else None
            if task.resolved and not task.category:
                task.category = category_for_item(task.resolved[0]).category
            if task.resolved:
                relative = task.resolved[0].relative_path or ""
                display = task.resolved[0].display_name or ""
                # `relative_path` is sometimes a directory and sometimes the full
                # filename. Resolve the containing directory precisely: taking
                # `.parent` unconditionally made an empty/relative filename yield
                # the task's parent directory, which then diverged the multipart
                # package_key across sibling parts.
                base = Path(task.destination)
                rel_path = Path(relative) if relative else None
                if rel_path is None:
                    folder = base
                elif display and rel_path.name == display:
                    folder = (base / rel_path).parent
                else:
                    folder = base / rel_path
                task.folder_path = str(folder.resolve())
                self._propagate_multipart_size(task)
            if task.provider and not task.account_ref:
                selected_account = self.dispatch("select_account", {"provider_id": task.provider})
                if selected_account:
                    task.account_ref = selected_account["id"]
            if task.account_ref:
                for item in task.resolved:
                    item.metadata = {**item.metadata, "account_ref": task.account_ref}
            is_mp, pkey, pnum = self._task_multipart_info(task)
            selection_backend, canary_reason = self._canary_backend_override(task.id, requested_backend)
            if is_mp:
                # PIPE / MP: For multipart packages, preserve backend consistency: ensure all parts execute with 'rust'
                # unless explicitly incompatible with the items or route profile.
                # Do not let stale overrides or canary suppress rust for multipart packages.
                rust_selection = self.backend_selector.select_for_items(
                    task.resolved, route_profile, self._transfer_backends(), "rust"
                )
                if rust_selection.compatible and rust_selection.backend == "rust":
                    selection = rust_selection
                    selection_backend = "rust"
                    canary_reason = None
                else:
                    selection = self.backend_selector.select_for_items(
                        task.resolved, route_profile, self._transfer_backends(), selection_backend,
                        package_backend="rust",
                    )
            else:
                selection = self.backend_selector.select_for_items(
                    task.resolved, route_profile, self._transfer_backends(), selection_backend
                )
            selection_payload = _redact_diagnostic(selection.to_dict())
            self._log_task(task, "info" if selection.compatible else "warn", "backend_selection",
                           selection.reason, {"selection": selection_payload, "canary": canary_reason})
            if not selection.compatible or not selection.backend:
                raise ValueError(selection.reason)
            backend_name = selection.backend
            metrics.selected_backend = backend_name
            metrics.available = selection.available
            metrics.availability_reason = selection.capabilities.availability_reason if selection.capabilities else None
            metrics.start()
            metrics.record_connection()
            self.events.emit("TaskBackendSelected", task.id, {
                "backend": backend_name,
                "automatic": selection.automatic,
                "requested_backend": requested_backend,
                "canary_override": canary_reason,
                "reason": selection.reason,
                "capabilities": selection.capabilities.to_dict() if selection.capabilities else None,
            })
            effective_rate, effective_scope = self._effective_bandwidth_details(
                task.id, task.queue_id, task.provider,
                task.account_ref or ((task.resolved[0].metadata or {}).get("account_ref") if task.resolved else None),
            )
            await self.resources.set_bandwidth_rate(effective_rate)
            self.rust_backend.bandwidth_rate = effective_rate
            self._log_task(task, "info", "throttle", "Applied effective bandwidth profile",
                           {"rate_bytes_per_second": effective_rate, "scope": effective_scope,
                            "unit": "bytes_per_second"})
            attempt_provider = task.provider or "generic"
            self.store.record_provider_attempt(task.id, attempt_fingerprint, attempt_provider,
                                               route_profile["id"], "resolved")
            self.health_monitor.record(attempt_provider, "success", "resolve",
                                       self.plugins.manifests.get(attempt_provider, {}).get("version"))
            task.size = sum(item.size or 0 for item in task.resolved)
            self._log_task(task, "info", "resolve", "Resolved download plan",
                           {"provider": task.provider, "items": len(task.resolved), "bytes": task.size,
                            "items_detail": [{"provider": item.provider, "display_name": item.display_name,
                                              "size": item.size, "direct_url": item.direct_url,
                                              "content_type": (item.metadata or {}).get("content_type")}
                                             for item in task.resolved]})
            if task.size > self.resources.policy.max_package_size:
                raise ValueError(f"package exceeds configured limit ({task.size} bytes)")
            self._transition(task, "preflight", "TaskPreflight")
            first_item = task.resolved[0] if task.resolved else None
            self._transition_stage(task, "direct_link_acquired", {
                "source": "resolver",
                "provider": task.provider,
                "content_length": first_item.size if first_item else None,
                "accept_ranges": bool(first_item.headers.get("Accept-Ranges") or first_item.headers.get("accept-ranges")) if first_item and first_item.headers else None,
                "items": len(task.resolved),
            }, source="resolver:plan", refresh_only=True)
            destination_root = Path(task.destination).resolve()
            destination_root.mkdir(parents=True, exist_ok=True)
            required_space = sum(int(item.size or 0) for item in task.resolved)
            free_space = shutil.disk_usage(destination_root).free
            if free_space < required_space + self.resources.policy.min_free_space:
                raise OSError(f"not enough free space for task: need {required_space + self.resources.policy.min_free_space} bytes")
            if not task.resolved:
                raise ValueError("provider returned no downloadable items")
            if (
                critical_trace.active()
                and bool((task.browser_context or {}).get("diagnostic_resolution_only"))
            ):
                task.paused_reason = "Resolution benchmark stopped after real preflight"
                self.store.save(task)
                critical_trace.mark(
                    "diagnostic.preflight_held",
                    event="state",
                    identity=critical_trace.TraceIdentity(
                        package_id=pkey if is_mp else None,
                        task_id=task.id,
                        part_number=pnum if is_mp else None,
                        attempt_id=task.retry_count + 1,
                    ),
                    resolved_items=len(task.resolved),
                    direct_items=sum(1 for item in task.resolved if item.direct_url),
                )
                return
            if is_mp:
                await self._await_multipart_transfer_admission(
                    task,
                    pkey,
                    (urlsplit(task.source_url or "").hostname or "").lower(),
                )
            # Reserve quota only after all local preflight checks pass.  A
            # rejected destination must not consume the user's daily budget.
            if not self.store.reserve_daily_quota(task.size or 0, self.resources.policy.daily_byte_quota):
                raise ValueError("daily download byte quota exceeded")
            self.store.save_plan(task.id, task.provider, task.destination, task.size,
                                 self.resources.policy.to_dict(), task.password_ref,
                                 {"task_id": task.id, "items": [self.store._persisted_item(item) for item in task.resolved]})
            self._transition(task, "downloading", "TaskResolved")
            task.backend = backend_name
            task.attempt_count = max(task.attempt_count, task.retry_count + 1)
            self.store.save(task)
            self._propagate_multipart_size(task)
            self.events.emit("TaskAdmitted", task.id, {"expected_bytes": task.size, "provider": task.provider})
            self.events.emit("TaskStarted", task.id, {"backend": backend_name})
            display_title = task.display_name or task.source_url.split("/")[-1].split("?")[0] or task.provider or task.id
            sess_logger = download_loggers.create(
                task_id=task.id,
                display_name=display_title,
                source_url=task.source_url,
                provider=task.provider or "generic",
                destination_path=str(task.destination),
                total_size=task.size or 0,
                backend=backend_name or "custom",
                streams_count=max(1, len(task.resolved or [])),
            )
            sess_logger.log_event("DISPATCH", f"Task started downloading with backend {backend_name}")
            completed_before_item = 0
            progress_lock = threading.Lock()
            item_progress_bytes: dict[int, int] = {}

            last_item_speed_sample: dict[int, tuple[float, float, float]] = {}  # index -> (smoothed_speed, timestamp, dip_start_time)

            def item_progress_cb(item_index: int, count: int) -> None:
                with progress_lock:
                    previous_count = item_progress_bytes.get(item_index, 0)
                    item_progress_bytes[item_index] = count
                    delta = max(0, count - previous_count)
                    now_mono = time.monotonic()
                    if delta:
                        metrics.record_read(delta)
                        metrics.record_write(delta)
                    # Update task.size dynamically if item sizes were discovered on the fly
                    current_task_size = sum(it.size or item_progress_bytes.get(i, 0) for i, it in enumerate(task.resolved))
                    if current_task_size > 0 and task.size != current_task_size:
                        task.size = current_task_size
                        self.store.save(task)
                        self._propagate_multipart_size(task)
                    total_done = sum(item_progress_bytes.values())
                    self._progress(task, total_done, item_index, count)

                    # Microsecond speed tracking & Concurrency Auditor progress feed
                    if item_index < len(task.resolved):
                        it = task.resolved[item_index]
                        h = (urlsplit(it.direct_url or it.source_url).hostname or "").lower()
                        if h:
                            previous_sample = last_item_speed_sample.get(item_index)
                            if previous_sample is None:
                                # The first callback has no valid interval. Treating
                                # its entire prefix as one millisecond fabricated a
                                # multi-GB/s spike and poisoned the following EMA.
                                prev_speed, dip_start = 0.0, 0.0
                                smoothed_speed = 0.0
                            else:
                                prev_speed, prev_time, dip_start = previous_sample
                                time_diff = max(0.001, now_mono - prev_time)
                                inst_speed = delta / time_diff
                                # Exponential moving average (alpha=0.3) to smooth chunk boundaries
                                smoothed_speed = inst_speed if prev_speed <= 0 else (0.3 * inst_speed + 0.7 * prev_speed)
                            
                            # Only treat as a sustained speed dip if smoothed speed remains >50% depressed for >= 2.0s
                            if prev_speed > 2 * 1024 * 1024 and smoothed_speed < prev_speed * 0.5:
                                if dip_start == 0.0:
                                    dip_start = now_mono
                                elif now_mono - dip_start >= 2.0:
                                    concurrency_auditor.record_speed_dip(
                                        h, f"{task.id}:{item_index}", smoothed_speed, prev_speed,
                                        f"Sustained throughput drop (>50% for {now_mono - dip_start:.1f}s) during concurrent transfer on {h}"
                                    )
                                    dip_start = 0.0
                            else:
                                dip_start = 0.0

                            last_item_speed_sample[item_index] = (smoothed_speed, now_mono, dip_start)
                            concurrency_auditor.update_stream_progress(h, f"{task.id}:{item_index}", count, smoothed_speed)
                            self.storage_concurrency.update_stream(h, f"{task.id}:{item_index}", count, smoothed_speed)

            async def _download_single_item(item_index: int, item: ResolvedItem) -> None:
                if task.state != "downloading":
                    self._transition(task, "downloading", "TaskDownloading")
                    self.store.save(task)
                self._log_task(task, "info", "download", f"Starting item {item_index + 1}",
                               {"item_index": item_index, "provider": item.provider,
                                "display_name": item.display_name, "bytes": item.size})
                sess_log = download_loggers.get(task.id)
                if sess_log:
                    sess_log.log_event("ITEM_START", f"Starting item {item_index + 1}: {item.display_name}", {"bytes": item.size})
                attempt_provider = item.provider
                self.store.record_provider_attempt(task.id, attempt_fingerprint, attempt_provider,
                                                   route_profile["id"], "started")
                lease_id = str(uuid4())
                host_name = (urlsplit(item.direct_url or item.source_url).hostname or "unknown").lower()
                self.store.create_lease(TransferLease(lease_id, task.id, host_name, item.provider,
                                                      (item.metadata or {}).get("account_ref"), 1, time.time() + 3600))
                leases.append(lease_id)
                stream_id = f"{task.id}:{item_index}"
                concurrency_auditor.record_stream_started(host_name, stream_id)
                concurrency_auditor.detect_preemption(host_name, stream_id)
                item_progress = lambda count, index=item_index: item_progress_cb(index, count)
                media_plan_raw = (item.metadata or {}).get("media_plan")
                if media_plan_raw:
                    media_segments = [MediaSegment(**{key: value for key, value in segment.items()
                                                      if key in MediaSegment.__dataclass_fields__})
                                      for segment in media_plan_raw.get("segments", [])]
                    media_plan = MediaPlan(media_plan_raw.get("kind", "hls"),
                                           media_plan_raw.get("manifest_url", media_plan_raw.get("url", task.source_url)),
                                           segments=media_segments, variants=media_plan_raw.get("variants", []),
                                           selected_variant=media_plan_raw.get("selected_variant"),
                                           encrypted=bool(media_plan_raw.get("encrypted")),
                                           output_format=media_plan_raw.get("output_format"),
                                           total_duration=media_plan_raw.get("total_duration"))
                    output_path = (Path(task.destination) / (item.relative_path or "") / item.display_name).resolve()
                    destination_root = Path(task.destination).resolve()
                    if destination_root != output_path and destination_root not in output_path.parents:
                        raise ValueError("media output path escapes task destination")
                    downloaded_path = await asyncio.to_thread(
                        self.media_assembler.assemble, media_plan, output_path,
                        pause=lambda: control.pause.is_set(),
                        progress=lambda done, total: item_progress(done),
                        ffmpeg=secrets.get("ffmpeg_path"))
                    downloaded_path = Path(downloaded_path["output"])
                else:
                    storage_host = (urlsplit(item.direct_url or item.source_url or "").hostname or "").lower()
                    storage_sem = self.storage_concurrency.get_semaphore(storage_host)
                    is_locked = storage_sem.locked() if hasattr(storage_sem, "locked") else (getattr(storage_sem, "_value", 1) <= 0)
                    if is_locked:
                        task.user_challenge = {
                            **(task.user_challenge or {}),
                            "waiting_storage_slot": True,
                        }
                        self.store.save(task)
                        self.events.emit("TaskUpdated", task.id, {"user_challenge": task.user_challenge})
                    async with storage_sem:
                        if (task.user_challenge or {}).get("waiting_storage_slot"):
                            task.user_challenge = {
                                k: v for k, v in task.user_challenge.items() if k != "waiting_storage_slot"
                            }
                            self.store.save(task)
                            self.events.emit("TaskUpdated", task.id, {"user_challenge": task.user_challenge})
                        # Size lets the stall policy skip files too small to judge
                        # and files already nearly done.
                        self.storage_concurrency.register_stream(
                            storage_host, stream_id, expected_bytes=int(item.size or 0))
                        try:
                            downloaded_path = await self._download_item_with_refresh(
                                task, item_index, item, task.destination, item_progress, control,
                                route_profile, backend_name, secrets, metrics)
                        finally:
                            self.storage_concurrency.unregister_stream(storage_host, stream_id)
                total_items = len(task.resolved) if task.resolved else 1
                is_last_item = (item_index >= total_items - 1)
                sess_log = download_loggers.get(task.id)
                if sess_log:
                    sess_log.phase = "verifying" if is_last_item else "downloading"
                    sess_log._stall_start_time = None
                if is_last_item:
                    self._transition(task, "verifying", "TaskVerifying", f"Verifying item {item_index + 1} integrity")
                checksum_algorithm, _, expected_checksum = item.checksum.partition(":") if item.checksum else ("sha256", "", None)
                self._transition_stage(task, "verifying_integrity" if is_last_item else "downloading", {
                    "source": "integrity",
                    "algorithm": checksum_algorithm or "sha256",
                    "item": item.display_name,
                    "item_index": item_index,
                    "expected_checksum": expected_checksum is not None,
                }, source="integrity:verify", refresh_only=True)
                self.store.save(task)
                if is_last_item:
                    self.events.emit("TaskUpdated", task.id, {"state": "verifying"})

                integrity = await asyncio.to_thread(
                    verify_file,
                    downloaded_path,
                    expected_size=item.size,
                    expected_checksum=expected_checksum,
                    algorithm=checksum_algorithm or "sha256",
                    content_type=(item.metadata or {}).get("content_type"),
                    etag=(item.metadata or {}).get("etag"),
                    last_modified=(item.metadata or {}).get("last_modified"),
                )
                if integrity.state in {"corrupt", "provider_rejected"}:
                    raise RuntimeError(integrity.reason or "download integrity validation failed")
                sess_log = download_loggers.get(task.id)
                if sess_log:
                    sess_log.log_event("ITEM_VERIFIED", f"Item {item_index + 1} integrity: {integrity.state}", {"reason": integrity.reason})
                with progress_lock:
                    task.integrity[item.item_id or f"{item_index}:{item.display_name}"] = integrity.to_dict()
                    self.store.save(task)
                if downloaded_path and downloaded_path.is_file():
                    actual_sz = downloaded_path.stat().st_size
                    if item.size is None or item.size != actual_sz:
                        item.size = actual_sz
                with progress_lock:
                    item_progress_bytes[item_index] = item.size or item_progress_bytes.get(item_index, 0)
                    task.size = sum(it.size or item_progress_bytes.get(i, 0) for i, it in enumerate(task.resolved))
                    total_done = sum(item_progress_bytes.values())
                    self._progress(task, total_done, item_index, item.size or item_progress_bytes.get(item_index, 0))
                self.store.save_segment(task.id, item_index, 0, 0, item.size or total_done, True, "completed", item.size or total_done)
                self.store.save_item_progress(task.id, item.item_id or f"{item_index}:{item.display_name}",
                                              item.size or 0, "completed")
                if downloaded_path and downloaded_path.is_file():
                    if item.checksum:
                        digest = getattr(integrity, "observed_checksum", None)
                        if not digest:
                            digest = await asyncio.to_thread(file_digest, downloaded_path)
                        self.store.save_hash(task.id, item.item_id or f"{item_index}:{item.display_name}", "sha256", digest, str(downloaded_path))
                self.store.record_provider_attempt(task.id, attempt_fingerprint, attempt_provider,
                                                   route_profile["id"], "completed")
                self.health_monitor.record(attempt_provider, "success", "download",
                                           self.plugins.manifests.get(attempt_provider, {}).get("version"))

            provider_limit = self.resources.policy.provider_limits.get(task.provider or "", self.resources.policy.per_provider_transfers)
            host_for_task = (urlsplit(task.source_url).hostname or "").lower()
            if host_for_task:
                prof = concurrency_auditor.get_profile(host_for_task)
                in_cd, _, _ = concurrency_auditor.is_host_in_cooldown(host_for_task)
                if in_cd:
                    effective_item_limit = max(1, prof.verified_ceiling)
                elif prof.denied_at_ceiling is not None:
                    effective_item_limit = max(1, min(provider_limit, prof.verified_ceiling))
                else:
                    effective_item_limit = provider_limit
            else:
                effective_item_limit = provider_limit

            # Check item target and storage hosts for single-stream limits
            for it in task.resolved:
                it_host = (urlsplit(it.direct_url or it.source_url or "").hostname or "").lower()
                if it_host:
                    st_lim = self.storage_concurrency.get_limit(it_host)
                    if st_lim <= 1:
                        effective_item_limit = 1
                        break
                    it_prof = concurrency_auditor.get_profile(it_host)
                    in_cd, _, _ = concurrency_auditor.is_host_in_cooldown(it_host)
                    if in_cd or (it_prof.denied_at_ceiling is not None and it_prof.verified_ceiling <= 1):
                        effective_item_limit = 1
                        break

            # Authoritative pressure-breaker ceiling for this host (if any).
            breaker_limit = concurrency_auditor.breaker_admission_limit(host_for_task) if host_for_task else None
            if breaker_limit is not None:
                effective_item_limit = max(1, min(effective_item_limit, breaker_limit))

            item_concurrency = max(1, min(len(task.resolved), effective_item_limit))
            sem = asyncio.Semaphore(item_concurrency)

            async def _worker(idx: int, item_obj: ResolvedItem) -> None:
                async with sem:
                    host_nm = (urlsplit(item_obj.direct_url or item_obj.source_url).hostname or "unknown").lower()
                    stream_key = f"{task.id}:{idx}"
                    if item_concurrency > 1:
                        # The breaker verdict is authoritative: wait out its
                        # cooldown instead of proceeding after a single sleep.
                        # A bounded total wait prevents an infinite stall if the
                        # host never reopens; the frozen-window backstops in the
                        # resource layer still gate the actual transfer permits.
                        gate_deadline = time.monotonic() + 300.0
                        while True:
                            active_on_host = len(self.storage_concurrency._active_streams.get(host_nm, {}))
                            allowed, _target, reason = concurrency_auditor.admission_decision(
                                host_nm, task_id=stream_key, active=active_on_host, max_global=provider_limit)
                            if allowed:
                                break
                            snap = concurrency_auditor.breaker_snapshot(host_nm)
                            wait_s = min(max(float(snap.get("cooldown_remaining") or 1.0), 0.5), 30.0)
                            self._log_task(task, "info", "backpressure",
                                           f"Host {host_nm} pressure gate: {reason}",
                                           {"host": host_nm, "reason": reason, "wait_seconds": wait_s,
                                            "state": snap.get("state")})
                            if control.cancel.is_set() or control.pause.is_set():
                                return
                            if time.monotonic() + wait_s > gate_deadline:
                                self._log_task(task, "warn", "backpressure",
                                               f"Host {host_nm} still gated after bounded wait; proceeding",
                                               {"host": host_nm, "reason": reason,
                                                "state": snap.get("state")})
                                break
                            await asyncio.sleep(wait_s)
                    try:
                        await _download_single_item(idx, item_obj)
                    finally:
                        concurrency_auditor.record_stream_finished(host_nm, stream_key)

            await asyncio.gather(*[_worker(i, it) for i, it in enumerate(task.resolved)])
            integrity_states = [str(value.get("state", "unverifiable")) for value in task.integrity.values()]
            # A task is only "verified" when every item's CONTENT was proven by a
            # checksum. If any item was merely length-checked the task reports
            # `size_verified`, which is weaker on purpose: providers that publish
            # no checksum cannot give us content proof, and claiming otherwise is
            # how a corrupt volume reached the extractor wearing a verified badge.
            if integrity_states and all(state == "verified" for state in integrity_states):
                task.integrity_state = "verified"
            elif integrity_states and all(
                state in {"verified", "size_verified"} for state in integrity_states
            ):
                task.integrity_state = "size_verified"
            else:
                task.integrity_state = "unverifiable"
            metrics.set_integrity(task.integrity_state)
            self._transition(task, "verifying", "TaskVerifying")
            self._transition(task, "postprocessing", "TaskPostprocessing")
            for lease_id in leases:
                self.store.release_lease(lease_id)
            # Publish the terminal transport snapshot before committing the
            # completed state.  Consumers may observe the state transition
            # immediately, so the durable outbox must already contain the
            # matching metrics event when that state becomes visible.
            self.events.emit("TaskTransportMetrics", task.id, metrics.finish(task.id, task.integrity_state) or {})
            log_files = download_loggers.finalize(task.id, status="completed", integrity_state=task.integrity_state)
            if log_files:
                self._log_task(task, "info", "benchmark_log", f"Download diagnostic log saved to {log_files['readable_log']}", log_files)
            # EOF-03: For indeterminate streams the Rust backend reports the
            # actual byte count only after transfer completes.  Accumulate
            # across all items and back-fill both task.size and each
            # ResolvedItem.size so the DB record is internally consistent.
            final_bytes = max(task.completed_bytes or 0, sum(item_progress_bytes.values()))
            if not (task.size and task.size > 0):
                task.size = final_bytes
                task.completed_bytes = final_bytes
                for _it in task.resolved or []:
                    if _it.size is None or _it.size == 0:
                        _it.size = final_bytes // max(1, len(task.resolved))
                self.store.save(task)
            elif task.completed_bytes != task.size:
                task.completed_bytes = task.size
                self.store.save(task)
            self._transition(task, "completed", "TaskCompleted")
            await self._on_download_completed(task)
            self._log_task(task, "info", "complete", "Download completed", {"bytes": task.completed_bytes})
        except (NeedsCaptcha, NeedsUser) as exc:
            action = getattr(exc, "action", "")
            raw_challenge = dict(getattr(exc, "challenge", {}) or {})
            continuation = raw_challenge.pop("_engine_continuation", None)
            exc.challenge = raw_challenge
            if isinstance(continuation, dict):
                self._provider_continuations[task.id] = continuation
                # Public, non-sensitive capability flag. The browser solver may
                # return immediately with a token because the engine retained the
                # provider's original step-two operation in private memory.
                raw_challenge["provider_resume"] = True
                exc.challenge = raw_challenge
                critical_trace.mark(
                    "resolve.provider_continuation.saved", event="state",
                    resource="provider", resource_id=(urlsplit(task.source_url).hostname or "").lower(),
                )
            else:
                # A provider that raises a new challenge without replacement has
                # invalidated the prior suspended operation.
                self._provider_continuations.pop(task.id, None)
            rejection_reason = str(raw_challenge.get("response_reason") or "")
            rejection_status = raw_challenge.get("response_status")
            if rejection_reason:
                host_name = (urlsplit(task.source_url).hostname or "").lower()
                next_delay = self.resolve_stagger.observe_rejection(
                    host_name,
                    status_code=int(rejection_status) if rejection_status is not None else None,
                    reason=rejection_reason,
                )
                critical_trace.mark(
                    "resolve.host_stagger.feedback", event="state",
                    resource="resolve_host", resource_id=host_name,
                    outcome="rejection", reason=rejection_reason,
                    next_delay_seconds=next_delay,
                )
            is_captcha = (
                isinstance(exc, NeedsCaptcha) or
                action in {"captcha", "turnstile", "recaptcha", "recaptcha_v2", "recaptcha_v3", "hcaptcha", "cloudflare"} or
                (isinstance(raw_challenge, dict) and ("sitekey" in raw_challenge or "site_key" in raw_challenge))
            )
            if is_captcha:
                trace_mp, trace_package, trace_part = self._task_multipart_info(task)
                critical_trace.mark(
                    "captcha.challenge_detected",
                    event="state",
                    identity=critical_trace.TraceIdentity(
                        package_id=trace_package if trace_mp else None,
                        task_id=task.id,
                        part_number=trace_part if trace_mp else None,
                        attempt_id=task.retry_count + 1,
                    ),
                    action=action or "captcha",
                    provider=task.provider or attempt_provider,
                )
                # Asked again right after carrying an answer: the site rejected it.
                self._settle_task_challenge(task, False, "the site asked for a captcha again")
                c_params = exc.params if isinstance(exc, NeedsCaptcha) else dict(raw_challenge if isinstance(raw_challenge, dict) else {})
                if "sitekey" in c_params and "site_key" not in c_params:
                    c_params["site_key"] = c_params["sitekey"]
                if "url" in c_params and "page_url" not in c_params:
                    c_params["page_url"] = c_params["url"]
                elif "url" not in c_params and "page_url" in c_params:
                    c_params["url"] = c_params["page_url"]
                elif "url" not in c_params and "page_url" not in c_params:
                    c_params["url"] = task.source_url
                    c_params["page_url"] = task.source_url

                if action in {"turnstile", "cloudflare"} or "turnstile" in str(exc).lower() or ("site_key" in c_params and "turnstile" in str(raw_challenge).lower()):
                    default_type = CaptchaType.TURNSTILE
                elif action in {"hcaptcha"} or "hcaptcha" in str(exc).lower():
                    default_type = CaptchaType.HCAPTCHA
                elif action in {"recaptcha", "recaptcha_v2", "recaptcha_v3"}:
                    default_type = CaptchaType.RECAPTCHA_V2
                else:
                    default_type = CaptchaType.IMAGE_TEXT

                c_type = exc.captcha_type if isinstance(exc, NeedsCaptcha) else getattr(exc, "challenge", {}).get("captcha_type", default_type)
                c_timeout = exc.timeout if isinstance(exc, NeedsCaptcha) else float(getattr(exc, "challenge", {}).get("timeout_seconds", 90.0))
                c_id = getattr(exc, "challenge_id", None) or getattr(exc, "challenge", {}).get("challenge_id") or str(uuid4())
                is_mp, pkey, pnum = self._task_multipart_info(task)
                captcha_host = (urlsplit(task.source_url or "").hostname or "").lower()
                if is_mp and pkey:
                    # Exactly one durable challenge per (package_key, host): reuse the
                    # pending package challenge instead of prompting once per part.
                    challenge, _challenge_created = self._ensure_package_challenge(
                        task, pkey, captcha_host, c_params, c_type, c_timeout, c_id)
                else:
                    challenge = CaptchaChallenge(
                        id=c_id,
                        task_id=task.id,
                        provider_id=task.provider or "generic",
                        captcha_type=c_type,
                        params=c_params,
                        timeout_seconds=c_timeout,
                    )
                    self.store.save_captcha_challenge(challenge)
                    self.captcha._challenges[challenge.id] = challenge
                telemetry_bus.record(
                    level="WARN",
                    subsystem="engine:captcha",
                    message=(
                        f"[CAPTCHA_CHALLENGE] {task.display_name or task.source_url} requires "
                        f"{challenge.captcha_type} verification (Provider: {challenge.provider_id}, SiteKey: {c_params.get('site_key', 'none')})"
                    ),
                    context={
                        "task_id": task.id,
                        "challenge_id": challenge.id,
                        "provider": challenge.provider_id,
                        "captcha_type": str(challenge.captcha_type),
                        "page_url": c_params.get("page_url") or c_params.get("url"),
                        "site_key": c_params.get("site_key"),
                    },
                    tier="engine",
                )
                task.user_action = "turnstile" if challenge.captcha_type == CaptchaType.TURNSTILE else "captcha"
                task.user_challenge = challenge.to_dict()
                self.store.save(task)
                self._transition(task, "needs_user", "TaskNeedsUser", f"Waiting for {challenge.captcha_type} verification")
                self._transition_stage(task, "captcha_challenge_detected", {
                    "source": "provider",
                    "challenge_id": challenge.id,
                    "captcha_type": str(challenge.captcha_type),
                    "provider": challenge.provider_id,
                    "site_key": c_params.get("site_key"),
                }, source="provider:challenge")

                if is_mp:
                    # PIPE-02: For multipart packages, suppress automated headless solver cascade.
                    # Parent package/task is held in 'needs_user' and CaptchaChallengeRequired is emitted once per package.
                    self.events.emit("CaptchaChallengeRequired", task.id, {
                        "groupId": pkey,
                        "group_id": pkey,
                        "displayName": task.display_name or task.source_url,
                        "host": captcha_host,
                        "pageUrl": c_params.get("page_url") or c_params.get("url") or task.source_url,
                        "part": pnum,
                    }, dedupe_key=f"package_captcha_required:{pkey}")
                    if pkey and (pkey in self._armed_packages() or self._captcha_auto_solve_enabled()):
                        self._autosolve_armed_package(
                            pkey, captcha_host,
                            c_params.get("page_url") or c_params.get("url") or task.source_url or "",
                            challenge_id=challenge.id)
                    return

                # D-03: Do NOT block inside the transfer worker slot while waiting for solver/user!
                # Release scheduler concurrency permit immediately by returning from _run_task_async,
                # and handle solver cascade in a detached background task.
                async def _solve_in_background(bg_challenge: CaptchaChallenge, bg_task_id: str,
                                              bg_secrets: dict[str, Any], bg_control: _TaskControl,
                                              bg_backend: str, bg_group: str, bg_fingerprint: str,
                                              bg_provider: str) -> None:
                    try:
                        # Parked: the transfer slot is already free; the solve waits its turn.
                        solution = await self.challenge_scheduler.run(
                            bg_challenge, lambda: self.captcha.request_solution(bg_challenge))
                        bg_task = self.store.get(bg_task_id)
                        if not bg_task:
                            return
                        self._log_task(bg_task, "info", "captcha_answered",
                                       "Captcha answered; resuming to let the site check the answer",
                                       {"solver": bg_challenge.solver_used})
                        token_val = solution_token(solution) or (solution.get("text", "") if isinstance(solution, dict) else "")
                        # The site has not ruled yet: the task carries the answer to it,
                        # and its next outcome settles the challenge (_settle_task_challenge).
                        bg_task.user_action, bg_task.user_challenge = None, {
                            "challenge_id": bg_challenge.id,
                            "captcha_type": str(bg_challenge.captcha_type),
                            "verifying": True,
                            "generation": challenge_lifecycle.generation_of(bg_challenge),
                            "token_received": bool(solution_token(solution)),
                        }
                        self.store.save(bg_task)
                        self._transition(bg_task, "queued", "TaskQueued", "Captcha answered, checking it with the site")

                        resumed_secrets = {
                            **bg_secrets,
                            "captcha_solution": solution,
                            "turnstile_token": token_val,
                            "cf-turnstile-response": token_val,
                            "cf_clearance": solution.get("cf_clearance") if isinstance(solution, dict) else None,
                            "user_agent": solution.get("user_agent") if isinstance(solution, dict) else None,
                        }
                        self._futures[bg_task.id] = asyncio.run_coroutine_threadsafe(
                            self._enqueue_task(bg_task.id, resumed_secrets, bg_control, bg_backend, bg_group),
                            self._loop,
                        )
                    except Exception as solve_err:
                        bg_task = self.store.get(bg_task_id)
                        if bg_task:
                            bg_task.user_action = "captcha"
                            bg_task.user_challenge = bg_challenge.to_dict()
                            self.store.record_provider_attempt(bg_task.id, bg_fingerprint, bg_provider,
                                                               bg_task.route_profile_id or "direct", "needs_user", error=str(solve_err)[:500])
                            self._log_task(bg_task, "warn", "user_action", str(solve_err), {"action": "captcha", "challenge": bg_challenge.to_dict()})
                            self._transition(bg_task, "needs_user", "TaskNeedsUser", str(solve_err))

                asyncio.create_task(_solve_in_background(
                    challenge, task.id, secrets, control, backend_name, group, attempt_fingerprint, attempt_provider
                ))
                return
            else:
                task.user_action, task.user_challenge = exc.action, exc.challenge
                self.store.record_provider_attempt(task.id, attempt_fingerprint, attempt_provider,
                                                   task.route_profile_id or "direct", "needs_user", error=str(exc)[:500])
                self._log_task(task, "warn", "user_action", str(exc), {"action": exc.action, "challenge": exc.challenge})
                self._transition(task, "needs_user", "TaskNeedsUser", str(exc))
        except DownloadPaused:
            metrics.set_integrity("paused")
            download_loggers.finalize(task.id, status="paused", integrity_state="paused")
            self._transition(task, "paused", "TaskPaused", "Paused by user")
            self.events.emit("TaskTransportMetrics", task.id, metrics.finish(task.id, "paused") or {})
        except DownloadCanceled:
            latest = self.store.get(task.id)
            if latest and (latest.state == "queued" or (latest.error and "starved" in latest.error)):
                # Task was dropped by stall watchdog (e.g. STORAGE_HOST_STALL_DROPPED)
                # and re-queued for retry. Do not overwrite state to canceled,
                # do not trigger fallback.
                task.state = latest.state
                task.error = latest.error
                download_loggers.finalize(task.id, status="queued", integrity_state="unverifiable")
                return
            metrics.set_integrity("canceled")
            download_loggers.finalize(task.id, status="canceled", integrity_state="canceled")
            self._transition(task, "canceled", "TaskCanceled", "Canceled by user")
            self.events.emit("TaskTransportMetrics", task.id, metrics.finish(task.id, "canceled") or {})
        except Exception as exc:
            latest = self.store.get(task.id)
            is_stalled_drop = bool(
                latest and (
                    latest.state == "queued"
                    or (latest.error and ("starved" in latest.error or "STORAGE_HOST_STALL_DROPPED" in latest.error))
                )
            )
            if is_stalled_drop:
                # Task was already dropped and re-queued by stall watchdog.
                # Do NOT trigger backend fallback; the Rust backend did not fail,
                # the server starved the connection.
                if latest:
                    task.state = latest.state
                    task.error = latest.error
                download_loggers.finalize(task.id, status="queued", integrity_state="unverifiable")
                return

            if task.id in self._deleted_task_ids:
                metrics.set_integrity("canceled")
                download_loggers.finalize(task.id, status="canceled", integrity_state="canceled")
                return

            if control.cancel.is_set():
                # Task was explicitly canceled. Do NOT trigger backend fallback.
                metrics.set_integrity("canceled")
                download_loggers.finalize(task.id, status="canceled", integrity_state="canceled")
                self._transition(task, "canceled", "TaskCanceled", "Canceled by user")
                self.events.emit("TaskTransportMetrics", task.id, metrics.finish(task.id, "canceled") or {})
                return

            if task.id in self._deleted_task_ids:
                return

            text = str(exc).lower()
            status = getattr(exc, "status_code", None) or getattr(exc, "code", None) or getattr(exc, "status", None)
            failure_class = classify_failure(exc, status)

            is_mp, pkey, pnum = self._task_multipart_info(task)

            # Differentiate network/storage stalls and server starvation from actual binary/engine failures.
            # Server starvation, timeouts, and connection resets are network/host issues, NOT Rust backend failures.
            is_stall_or_network = (
                failure_class in {
                    FailureClass.TRANSIENT_NETWORK,
                    FailureClass.RATE_LIMITED,
                    FailureClass.QUOTA,
                    FailureClass.EXPIRED_URL,
                    FailureClass.EXPIRED_SESSION,
                }
                or any(m in text for m in (
                    "starved", "stall", "timeout", "timed out", "connection reset",
                    "connection aborted", "connection refused", "reset by peer",
                    "broken pipe", "remotedisconnected", "remote end closed",
                    "storage_host_stall_dropped", "unexpected eof", "read timed out",
                    "incomplete read",
                ))
            )

            # Only trigger backend fallback for genuine engine/binary failures on non-multipart tasks.
            # Multipart tasks must maintain consistency with 'rust' unless incompatible.
            # Stalls, timeouts, rate limits, and watchdog drops must retry with rust, not fall back to custom.
            if not is_mp and not is_stall_or_network:
                canary_state = self._record_canary_failure(task.id, backend_name or "", failure_class, str(exc))
                safe_fallback_backend = (canary_state or {}).get("fallback_backend", "custom")
                if requested_backend is None and backend_name and backend_name != safe_fallback_backend:
                    durable_progress = bool(task.completed_bytes) or any(
                        bool(segment.get("completed_bytes")) or bool(segment.get("completed"))
                        for segment in self.store.list_segments(task.id)
                    )
                    if not durable_progress:
                        old_backend = backend_name
                        backend_name = safe_fallback_backend
                        task.backend = backend_name
                        metrics.selected_backend = backend_name
                        self._log_task(task, "warn", "backend_fallback",
                                       f"Falling back from {old_backend} to {backend_name} before durable transfer progress",
                                       {"from_backend": old_backend, "to_backend": backend_name,
                                        "failure_class": failure_class.value, "checkpoint": "pre-transfer",
                                        "canary": bool(canary_state)})
                        self.events.emit("TaskBackendFallback", task.id, {
                            "from_backend": old_backend,
                            "to_backend": backend_name,
                            "failure_class": failure_class.value,
                            "checkpoint": "pre-transfer",
                            "canary": bool(canary_state),
                        })
                        task.error = None
                        self.store.save(task)
                        fallback_ctrl = _TaskControl() if control.cancel.is_set() else control
                        self._controls[task.id] = fallback_ctrl
                        self._requeue_task_later(task.id, secrets, fallback_ctrl, backend_name, group)
                        return
                    self._log_task(task, "warn", "backend_fallback",
                                   "Refused backend fallback after transfer progress was written",
                                   {"from_backend": backend_name, "to_backend": safe_fallback_backend,
                                    "failure_class": failure_class.value, "checkpoint": "unsafe",
                                    "canary": bool(canary_state)})
            # Confirmed host pressure (429/Retry-After/403-throttle/Cloudflare
            # idle/abort-after-partial-chunk) must reach the breaker even when
            # the quota path could also match; explicit quota types are excluded
            # because an account quota is not host pressure.
            host_nm = (urlsplit(task.source_url).hostname or "").lower()
            if host_nm:
                next_delay = self.resolve_stagger.observe_rejection(
                    host_nm,
                    status_code=int(status) if status is not None else None,
                    reason=str(getattr(exc, "category", "") or text[:128]),
                )
                critical_trace.mark(
                    "resolve.host_stagger.feedback", event="state",
                    resource="resolve_host", resource_id=host_nm,
                    outcome="failure", status=int(status) if status is not None else None,
                    next_delay_seconds=next_delay,
                )
            quota_typed = (
                isinstance(exc, QuotaExceeded) or
                (isinstance(exc, ProviderMappedError) and getattr(exc, "category", "") == "quota")
            )
            pressure_reason: str | None = None
            if host_nm and not quota_typed:
                pressure_reason = concurrency_auditor.note_pressure_failure(
                    host_nm, task.id,
                    status_code=int(status) if status is not None else None,
                    retry_after=float(getattr(exc, "retry_after", 0.0) or 0.0),
                    response_text=str(exc)[:512],
                    error_text=text[:512],
                    partial_chunks=int(getattr(exc, "partial_chunks", 0) or 0),
                )
            is_quota = (
                quota_typed or
                any(marker in text for marker in ("quota", "bandwidth limit", "download limit", "traffic exhausted", "exceeded limit")) or
                (status in {429, 509} and pressure_reason is None)
            )

            if is_quota:
                # 1. Permanently lock quota for this provider / URL (no backtracking)
                self.fallback.lock_quota(
                    task.id,
                    attempt_provider,
                    task.source_url,
                    route_profile_id=task.route_profile_id or "direct",
                    retry_after=getattr(exc, "retry_after", None),
                    error=str(exc)[:500],
                )
                self._log_task(task, "warn", "quota_locked", f"Provider {attempt_provider} hit quota. Excluded from future task attempts.",
                               {"provider": attempt_provider, "url": task.source_url, "error": str(exc)})

                # 2. Check for alternative mirror URLs that have not been quota'd
                alternate_candidates = self.fallback.candidates(
                    task.id,
                    task.alternate_urls or [],
                    route_profile_id=task.route_profile_id or "direct",
                    strict_quota_exclusion=True,
                )

                if alternate_candidates:
                    next_cand = alternate_candidates[0]
                    next_url = next_cand["url"]
                    next_prov = next_cand.get("provider_id", "generic")
                    self._log_task(task, "info", "provider_failover",
                                   f"Failing over from {attempt_provider} to alternate mirror {next_prov}",
                                   {"from_provider": attempt_provider, "to_provider": next_prov, "next_url": next_url})
                    self.events.emit("TaskProviderFailover", task.id, {
                        "failed_provider": attempt_provider,
                        "next_provider": next_prov,
                        "next_url": next_url,
                        "reason": "quota_exceeded",
                    })
                    task.source_url = next_url
                    task.source_fingerprint = hashlib.sha256(next_url.encode("utf-8")).hexdigest()
                    task.provider = next_prov
                    task.error = None
                    self.store.save(task)
                    self._transition(task, "queued", "TaskQueued", "Failing over to alternate mirror after quota")
                    asyncio.run_coroutine_threadsafe(
                        self._enqueue_task(task.id, secrets, control, backend_name, (urlsplit(next_url).hostname or "unknown").lower()),
                        self._loop,
                    )
                    return

                # 3. If no alternative mirrors exist, attempt VPN route rotation (new IP address)
                route_settings = self._ui_settings().get("routes", {})
                next_route = None
                if route_settings.get("autoSwitchOnQuota", True):
                    next_route = self.routes.next_healthy_route(
                        task.id, current_route_id=task.route_profile_id,
                        include_proxies=bool(route_settings.get("switchIncludesProxies", True)),
                        allow_direct=bool(route_settings.get("allowDirectFallback", True)),
                    )
                else:
                    self._log_task(task, "info", "route_rotation_disabled",
                                   "Quota hit; switching routes is turned off in Connections settings", {})
                if next_route and next_route.id != (task.route_profile_id or "direct"):
                    self._log_task(task, "info", "route_rotation",
                                   f"Rotating route profile from {task.route_profile_id} to {next_route.id} to bypass IP quota",
                                   {"old_route": task.route_profile_id, "new_route": next_route.id})
                    self.events.emit("TaskRouteRotated", task.id, {
                        "from_route": task.route_profile_id,
                        "to_route": next_route.id,
                        "reason": "ip_quota_bypassed",
                    })
                    task.route_profile_id = next_route.id
                    task.error = None
                    self.store.save(task)
                    self._transition(task, "queued", "TaskQueued", f"Retrying on new route {next_route.id}")
                    asyncio.run_coroutine_threadsafe(
                        self._enqueue_task(task.id, secrets, control, backend_name, group),
                        self._loop,
                    )
                    return

                # 4. If no alternative mirror or route rotation is available, transition to needs_user with clear guidance
                prov_label = "Google" if "google" in str(attempt_provider).lower() or "gdrive" in str(attempt_provider).lower() else str(attempt_provider)
                quota_msg = f"Download quota reached: Add a {prov_label} account or switch VPN to retry"
                task.error = quota_msg
                self.store.save(task)
                self._transition(task, "needs_user", "TaskNeedsUser", quota_msg)
                self._log_task(task, "warn", "quota_needs_user", quota_msg)
                return

            # Central retry handling.  The specialized quota branch above gets
            # first chance to select a mirror or route; this bounded fallback
            # handles transport, expiry, and session failures consistently for
            # every downloader backend.
            retry_policy = RetryPolicy(max_attempts=max(1, int(self.resources.policy.max_retries)))
            decision = decide_retry(exc, task.retry_count, retry_policy, status, getattr(exc, "retry_after", None))
            if decision.retry:
                # The classifier already had first pass above; unclassified but
                # explicit rate/concurrency language still clamps the ladder.
                if host_nm and pressure_reason is None and (
                    status in {429, 503}
                    or any(marker in text for marker in ("rate", "concurr", "too many", "wait"))
                ):
                    concurrency_auditor.record_stream_denied(
                        host_nm, task.id, status_code=int(status) if status is not None else None,
                        response_text=str(exc)[:180],
                        cooldown_seconds=decision.delay_seconds or 180.0,
                    )
                task.retry_count += 1
                metrics.record_retry()
                safe_error = _redact_diagnostic(str(exc))
                self.store.record_retry(task.id, decision.failure.value, str(safe_error),
                                        status_code=int(status) if status is not None else None,
                                        delay_seconds=decision.delay_seconds)
                self.store.record_provider_attempt(task.id, attempt_fingerprint, attempt_provider,
                                                   task.route_profile_id or "direct", "retrying",
                                                   status_code=int(status) if status is not None else None,
                                                   retry_after=decision.delay_seconds,
                                                   error=str(safe_error)[:500])
                self.health_monitor.record(attempt_provider, "failed", "transfer", error=str(safe_error)[:500])
                self._transition(task, "retrying", "TaskRetrying",
                                 f"{decision.failure.value}: retry {task.retry_count}/{retry_policy.max_attempts}")
                sess_log = download_loggers.get(task.id)
                if sess_log:
                    sess_log.log_event("RETRY_SCHEDULED", f"{decision.failure.value}: retry {task.retry_count}/{retry_policy.max_attempts} (delay={decision.delay_seconds}s)")
                task.error = None
                self._transition(task, "queued", "TaskQueued", decision.reason)
                self._requeue_task_later(
                    task.id, secrets, control, backend_name,
                    (urlsplit(task.source_url).hostname or "unknown").lower(),
                    delay=min(60.0, decision.delay_seconds),
                )
                return

            # Standard terminal failure handling
            task.retry_count += 1
            metrics.set_integrity("failed")
            safe_error = _redact_diagnostic(str(exc))
            self.store.record_retry(task.id, decision.failure.value, str(safe_error),
                                    status_code=int(status) if status is not None else None)
            self.store.record_provider_attempt(task.id, attempt_fingerprint, attempt_provider,
                                               task.route_profile_id or "direct", "failed",
                                               status_code=int(status) if status is not None else None,
                                               error=str(safe_error)[:500])
            self.health_monitor.record(attempt_provider, "failed", "transfer", error=str(safe_error)[:500])
            self._log_task(task, "error", "failure", "Task failed", {"error": safe_error, "retry_count": task.retry_count,
                                                                        "failure_class": decision.failure.value})
            try:
                print(
                    f"\n[DOWNLOAD FAILED] Task: {task.id} | Name: {task.display_name or 'unnamed'} | Provider: {attempt_provider}\n"
                    f"  URL: {task.source_url}\n"
                    f"  Failure Class: {decision.failure.value} | Status: {status}\n"
                    f"  Reason: {safe_error}\n"
                    f"  Retry count: {task.retry_count}\n",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception:
                pass
            download_loggers.finalize(task.id, status="failed", error=str(safe_error), integrity_state="failed")
            self._transition(task, "failed", "TaskFailed", str(safe_error))
            self.events.emit("TaskTransportMetrics", task.id, metrics.finish(task.id, "failed") or {})
        finally:
            self._clear_canary_operation(task.id)
            for lease_id in leases:
                self.store.release_lease(lease_id)
            latest = self.store.get(task.id)
            if latest and (latest.state == "queued" or (latest.error and "starved" in latest.error)):
                pass
            else:
                self.store.save(task)

    @staticmethod
    def _archive_package_key(task: DownloadTask, path: Path, info: Any) -> str:
        identity = f"{task.id}|{path.parent}|{info.base_name.casefold()}|{info.format_type}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _archive_event(self, event_type: str, job: ArchiveJob, *, reason: str | None = None) -> None:
        payload = {
            "job_id": job.id,
            "package_key": job.package_key,
            "operation": job.operation,
            "state": job.state,
            "progress_bytes": job.progress_bytes,
            "expected_size": job.expected_size,
            "observed_size": job.observed_size,
            "observed_digest": job.observed_digest,
            "error": _redact_diagnostic(job.error) if job.error else None,
        }
        if reason:
            payload["reason"] = reason
        self.events.emit(event_type, job.task_id or None, payload,
                         f"archive:{job.id}:{event_type}:{job.revision}")
        self._apply_archive_stage(event_type, job)

    def _archive_stage_target(self, job: ArchiveJob) -> DownloadTask | None:
        if job.task_id:
            target = self.store.get(job.task_id)
            if target:
                return target
        if job.package_key:
            package_tasks = [t for t in self.store.list() if t.package_key == job.package_key]
            leader = next((t for t in package_tasks if t.id == job.package_leader_id), None)
            target = leader or (package_tasks[0] if package_tasks else None)
            if target:
                return target
        telemetry_bus.record(
            level="DEBUG",
            subsystem="engine:archive",
            message=f"[ARCHIVE_STAGE_ORPHAN] Archive event {job.id} has no live task for lifecycle stage update",
            context={"job_id": job.id, "package_key": job.package_key, "task_id": job.task_id},
        )
        return None

    def _apply_archive_stage(self, event_type: str, job: ArchiveJob) -> None:
        stage = lifecycle.ARCHIVE_EVENT_STAGE_MAP.get(event_type)
        if event_type not in lifecycle.ARCHIVE_EVENT_STAGE_MAP:
            telemetry_bus.record(
                level="DEBUG",
                subsystem="engine:archive",
                message=f"[ARCHIVE_EVENT_UNMAPPED] Archive event '{event_type}' has no lifecycle stage mapping",
                context={"job_id": job.id, "event_type": event_type},
            )
            return
        target = self._archive_stage_target(job)
        if target is None:
            return
        detail = lifecycle.archive_event_detail(event_type, job.to_dict())
        if event_type == "ArchiveQueued":
            detail["parts"] = len(job.part_manifest or [])
        if stage is None:
            # Detail-only refresh (progress ticks, failures) — never a silent bail:
            # the failure/progress detail is recorded on the current stage.
            if target.stage:
                self._transition_stage(target, target.stage, detail, source=f"archive:{event_type}", refresh_only=True)
            else:
                self._log_task(target, "debug", "lifecycle",
                               f"Archive event '{event_type}' arrived before any lifecycle stage existed",
                               {"event_type": event_type, "job_id": job.id})
            return
        self._transition_stage(target, stage, detail, source=f"archive:{event_type}")

    def _report_incomplete_package(self, task: DownloadTask, candidate: Path, info: Any) -> None:
        """Surface a multipart package whose volume set has a hole.

        `is_package_complete` requires contiguity from part 1. A gap means
        extraction is impossible, so the user has to re-fetch the missing volume;
        staying quiet here is the silent cascade bail AGENTS.md forbids. Sources
        are never deleted on this path -- no archive job is created at all.
        """
        siblings = MultiPartDetector.find_package_siblings(candidate.parent, info.base_name, info.format_type)
        found = sorted({number for number, _path in siblings})
        highest = max(found) if found else 0
        missing = [number for number in range(1, highest + 1) if number not in found]
        detail = {
            "package": info.base_name,
            "format": info.format_type,
            "directory": str(candidate.parent),
            "found_parts": found,
            "missing_parts": missing,
            "highest_part_seen": highest,
        }
        self._log_task(
            task, "warning", "archive",
            f"[ARCHIVE_PACKAGE_INCOMPLETE] Incomplete package '{info.base_name}': "
            f"missing volume(s) {missing or '(trailing volume(s) after ' + str(highest) + ')'}; "
            f"extraction skipped and every downloaded volume kept",
            detail,
        )
        self.events.emit(
            "ArchivePackageIncomplete",
            task.id,
            {"task_id": task.id, **detail},
            dedupe_key=f"archive_incomplete:{info.base_name}:{candidate.parent}",
        )

    def _apply_archive_cleanup_result(self, job: ArchiveJob, deleted_count: int) -> None:
        target = self._archive_stage_target(job)
        if target is None:
            return
        self._transition_stage(target, "completed", {
            "source": "archive",
            "cleanup_deleted_files": deleted_count,
            "operation": job.operation,
        }, source="archive:cleanup_done")

    @staticmethod
    def _archive_source_paths(job: ArchiveJob) -> set[str]:
        sources = {str(Path(entry.get("path", "")).resolve()) for entry in (job.part_manifest or []) if entry.get("path")}
        if job.input_path:
            sources.add(str(Path(job.input_path).resolve()))
        return sources

    def _archive_produced_files(self, output: Path, job: ArchiveJob) -> list[Path]:
        """Files produced by extraction, excluding sources and staging leftovers."""
        entries = produced_file_manifest(output, self._archive_source_paths(job))
        return [Path(entry["path"]) for entry in entries]

    async def _cleanup_archive_sources(self, job: ArchiveJob) -> int | None:
        """Idempotent, symlink-safe deletion of source volumes after verification.

        Returns the number deleted, or ``None`` when the sources are still locked
        (the job must remain in ``cleanup_pending`` so boot recovery retries).
        """
        candidates: list[Path] = [Path(entry.get("path", "")) for entry in (job.part_manifest or []) if entry.get("path")]
        if job.input_path:
            candidates.append(Path(job.input_path))
        existing = [path for path in candidates if path.is_file() and not path.is_symlink()]
        if existing and not ArchiveBackend.wait_for_path_release(existing, timeout=30.0):
            telemetry_bus.record(
                level="WARN",
                subsystem="engine:archive",
                message="[ARCHIVE_CLEANUP_BLOCKED] Source volumes are still locked after extraction",
                context={"job_id": job.id, "paths": [str(path) for path in existing][:8]},
            )
            return None
        deleted = 0
        failed: list[str] = []
        for part_path in existing:
            if await self._unlink_with_retry(part_path, attempts=5):
                deleted += 1
            else:
                failed.append(str(part_path))
        if failed:
            # Undeleted sources keep the job in cleanup_pending so boot recovery
            # retries; reporting completion here would leak the source volumes.
            telemetry_bus.record(
                level="WARN",
                subsystem="engine:archive",
                message="[ARCHIVE_CLEANUP_INCOMPLETE] Source volumes remain after cleanup; will retry on next boot",
                context={"job_id": job.id, "deleted": deleted, "failed_count": len(failed),
                         "failed": failed[:8]},
            )
            return None
        return deleted

    @staticmethod
    async def _unlink_with_retry(path: Path, attempts: int = 5) -> bool:
        """Delete a file, retrying transient Windows sharing violations."""
        delay = 0.25
        for attempt in range(attempts):
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return True
            except OSError as err:
                if attempt == attempts - 1:
                    telemetry_bus.record(
                        level="WARN",
                        subsystem="engine:archive",
                        message="[ARCHIVE_DELETE_FAILED] Gave up deleting a source archive file",
                        context={"path": str(path), "attempts": attempts,
                                 "winerror": getattr(err, "winerror", None), "error": str(err)[:200]},
                    )
                    return False
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 4.0)
        return False

    def _refresh_archive_manifest(self, job: ArchiveJob) -> ArchiveJob | None:
        """Rebuild the archive manifest from the on-disk volume set when it grew.

        A job can be created before every volume finishes downloading; a stale
        manifest would leave later volumes unextracted and undeleted.
        """
        if not job.input_path:
            return None
        info = MultiPartDetector.detect(job.input_path)
        if not info or not info.is_multipart:
            return None
        siblings = MultiPartDetector.find_package_siblings(
            Path(job.input_path).parent, info.base_name, info.format_type)
        if len(siblings) <= len(job.part_manifest or []):
            return None
        manifest = [{"part_number": number, "path": str(path), "size": path.stat().st_size}
                    for number, path in siblings]
        job.part_manifest = manifest
        job.expected_size = sum(int(entry["size"]) for entry in manifest)
        job = self.store.update_archive_job(job)
        self._archive_event("ArchiveProgress", job, reason="manifest_refreshed")
        return job

    def _schedule_archive_job(self, job_id: str) -> None:
        """Submit a durable archive job without blocking a transfer worker."""
        if not self._loop.is_running():
            return
        if job_id in self._archive_inflight:
            telemetry_bus.record(
                level="DEBUG",
                subsystem="engine:archive",
                message="[ARCHIVE_SCHEDULE_DEDUPED] Archive job is already in flight",
                context={"job_id": job_id},
            )
            return
        self._archive_inflight.add(job_id)
        try:
            future = asyncio.run_coroutine_threadsafe(self._enqueue_archive_job(job_id), self._loop)
        except Exception:
            self._archive_inflight.discard(job_id)
            raise
        self._archive_futures.add(future)
        future.add_done_callback(self._archive_futures.discard)

    async def _enqueue_archive_job(self, job_id: str) -> None:
        if self._scheduler is None:
            self._archive_inflight.discard(job_id)
            raise RuntimeError("transfer scheduler is not ready")
        job = self.store.get_archive_job(job_id)
        if not job or job.state != "queued":
            self._archive_inflight.discard(job_id)
            return
        await self._scheduler.submit(
            f"archive:{job.package_key or job.id}",
            lambda: self._run_archive_job(job_id),
            priority=0,
            dedupe_key=f"archive:{job_id}",
        )

    async def _reconcile_stranded_archives(self) -> None:
        """Queue archiving for any finished package that never got a job.

        Archiving is triggered by the completion of the last part. If that single
        event is missed -- a race on the task's own state, a restart between the
        last download and the trigger, an exception on the way through -- nothing
        ever retries, and the user is left with a folder of .rar files and no
        error. This sweep makes the outcome depend on the state of the world
        rather than on one event firing at the right moment.
        """
        by_package: dict[str, list[DownloadTask]] = {}
        for candidate in self.store.list():
            if candidate.package_key:
                by_package.setdefault(candidate.package_key, []).append(candidate)
        if not by_package:
            return
        try:
            jobs_by_package: dict[str, list[str]] = {}
            for job in self.store.list_archive_jobs():
                jobs_by_package.setdefault(str(job.get("package_key") or ""), []).append(
                    str(job.get("state") or ""))
        except Exception as exc:
            logger.debug("Archive reconciliation could not list jobs: %s", exc)
            return
        retried = getattr(self, "_archive_retry_attempted", None)
        if retried is None:
            retried = set()
            self._archive_retry_attempted = retried
        for package_key, members in by_package.items():
            states = jobs_by_package.get(package_key)
            if states:
                # A job that FAILED must not block the package forever. Observed
                # live: an archive-worker crash left a `failed` job, and every
                # later re-download of the same package skipped archiving
                # entirely because a job "already existed" -- the user saw unrar
                # as simply disabled. Only a job that is finished or still doing
                # something counts as blocking.
                if any(state not in _ARCHIVE_RETRYABLE_STATES for state in states):
                    continue
                if package_key in retried:
                    continue
                retried.add(package_key)
            if any(t.state != "completed" or t.integrity_state not in _INTEGRITY_PASS_STATES
                   for t in members):
                continue
            if len(members) < 2:
                continue
            telemetry_bus.record(
                level="WARNING",
                subsystem="engine:archive",
                message=(
                    f"[ARCHIVE_RECONCILED] Package {package_key} finished with no archive job; "
                    f"queueing it now ({len(members)} parts)"
                ),
                context={"package_key": package_key, "parts": len(members)},
                tier="engine",
            )
            await self._on_download_completed(members[-1])

    async def _on_download_completed(self, task: DownloadTask) -> None:
        """Create at most one archive job for every completed multipart package."""
        if task.package_key:
            # Use the in-hand task for ITS OWN state. The caller transitions it to
            # `completed` immediately before calling us, and re-reading it from the
            # store raced that write: the last part could fail its own gate check,
            # and since no later event re-triggers the sweep, the whole package was
            # left unarchived. Observed live: three parts completed, zero archive
            # jobs, and no log line saying why.
            package_tasks = [t if t.id != task.id else task
                             for t in self.store.list() if t.package_key == task.package_key]
            if not any(t.id == task.id for t in package_tasks):
                package_tasks.append(task)
            if task.package_part_count and len(package_tasks) < task.package_part_count:
                self._log_task(task, "debug", "archive",
                               f"[ARCHIVE_DEFERRED_MISSING_PARTS] {len(package_tasks)} of "
                               f"{task.package_part_count} part tasks present; waiting",
                               {"package_key": task.package_key, "present": len(package_tasks),
                                "expected": task.package_part_count})
                return
            # `size_verified` is a legitimate pass here: most file hosts publish no
            # checksum, so requiring content proof would block extraction for every
            # such package. The extractor's own CRC is the content check in that
            # case -- which is exactly how a corrupt volume gets caught.
            pending = [t for t in package_tasks
                       if t.state != "completed" or t.integrity_state not in _INTEGRITY_PASS_STATES]
            if pending:
                # Never silent: this return is why a finished package can sit with
                # no archive job, and the log has to say so.
                self._log_task(task, "debug", "archive",
                               f"[ARCHIVE_DEFERRED_INCOMPLETE_PACKAGE] waiting on "
                               f"{len(pending)} of {len(package_tasks)} part(s)",
                               {"package_key": task.package_key,
                                "waiting_on": [(t.package_part_number, t.state, t.integrity_state)
                                               for t in pending][:8]})
                return

        packages: dict[str, tuple[Any, list[Path]]] = {}
        for item in task.resolved:
            rel = item.relative_path or item.display_name or ""
            if item.relative_path and item.display_name and Path(item.relative_path).name != item.display_name:
                rel = str(Path(item.relative_path) / item.display_name)
            candidate = (Path(task.destination).resolve() / rel).resolve()
            destination = Path(task.destination).resolve()
            if candidate != destination and destination not in candidate.parents:
                continue
            info = MultiPartDetector.detect(candidate.name)
            if not info:
                continue
            complete, parts = MultiPartDetector.is_package_complete(candidate.parent, info.base_name, info.format_type)
            if complete and len(parts) > 1:
                pkg_key = task.package_key or self._archive_package_key(task, candidate, info)
                packages.setdefault(pkg_key, (info, parts))
            elif not complete and parts:
                # Never a silent skip. An incomplete volume set can never be
                # extracted and only the user can resolve it; reporting nothing
                # here left the parts that DID land sitting on screen as finished
                # downloads, with no indication the package was unusable.
                self._report_incomplete_package(task, candidate, info)

        for package_key, (info, parts) in packages.items():
            first = parts[0]
            output_directory = str(first.parent)
            manifest = [{"part_number": number, "path": str(path), "size": path.stat().st_size}
                        for number, path in MultiPartDetector.find_package_siblings(first.parent, info.base_name, info.format_type)]
            expected_size = sum(int(part["size"]) for part in manifest)

            pkg_tasks = [t for t in self.store.list() if t.package_key == package_key] if package_key else [task]
            auto_extract = any((t.browser_context or {}).get("auto_extract", True) is not False for t in (pkg_tasks or [task]))
            if auto_extract is False:
                self.events.emit(
                    "ArchiveChoiceRequired",
                    task.id,
                    {
                        "task_id": task.id,
                        "input_path": str(first),
                        "output_directory": output_directory,
                        "expected_size": expected_size,
                        "package_key": package_key,
                    },
                    dedupe_key=f"archive_choice:{task.id}:{package_key}",
                )
                continue

            # Multi-part archives (e.g. .part01.rar, .r00, .7z) extract the archive into the parent folder.
            # Split binaries (.001) join together and verify.
            operation = "join_verify"
            if info.format_type in {"part_archive", "numbered_rar"} and self.archive_backend.available():
                operation = "extract"
            job = ArchiveJob(
                id=str(uuid4()), task_id=task.package_leader_id or task.id, input_path=str(first), output_directory=output_directory,
                format=info.format_type, password_ref=task.password_ref,
                policy=self.resources.policy.to_dict(), state="queued", package_key=package_key,
                operation=operation, part_manifest=manifest, expected_size=expected_size,
            )
            persisted, created = self.store.insert_or_get_archive_job(job)
            if created:
                self._archive_event("ArchiveQueued", persisted)
            if persisted.state == "queued":
                self._schedule_archive_job(persisted.id)

    async def _run_archive_job(self, job_id: str) -> None:
        job = self.store.get_archive_job(job_id)
        if not job or job.state in {"completed", "failed", "canceled"}:
            return
        permit = False
        control = self._archive_controls.setdefault(job_id, _TaskControl())
        try:
            job = self.store.transition_archive_job(job.id, "running")
            self._archive_event("ArchiveStarted", job)
            # Refresh the manifest from disk at execution time: the job may have
            # been created before every volume finished downloading, and a stale
            # manifest would leave later volumes unextracted and undeleted.
            refreshed = self._refresh_archive_manifest(job)
            if refreshed is not None:
                job = refreshed
            await self.resources.active_segments.acquire()
            permit = True
            parts = [Path(entry["path"]) for entry in job.part_manifest]
            if job.operation == "join_verify" and job.format == "split_binary":
                info = MultiPartDetector.detect(parts[0].name)
                if not info:
                    raise ValueError("multipart manifest no longer matches its source")
                output = parts[0].parent / info.base_name
                last_progress = [0]

                def progress(done: int, _total: int) -> None:
                    if done - last_progress[0] < 1024 * 1024 and done != job.expected_size:
                        return
                    last_progress[0] = done
                    current = self.store.get_archive_job(job.id)
                    if not current or current.state != "running":
                        return
                    current.progress_bytes = done
                    updated = self.store.update_archive_job(current)
                    self._archive_event("ArchiveProgress", updated)

                joined, digest, observed_size = await asyncio.to_thread(
                    BinaryPartJoiner.join, parts, output, 1024 * 1024, progress, False)
                job = self.store.get_archive_job(job.id) or job
                job.progress_bytes = observed_size
                job.observed_size = observed_size
                job.observed_digest = digest
                job.staging_path = str(joined)
                if job.expected_size is not None and observed_size != job.expected_size:
                    if joined.exists():
                        joined.unlink()
                    raise ValueError("joined package size does not match its manifest")
                if job.expected_digest and digest.casefold() != job.expected_digest.casefold():
                    if joined.exists():
                        joined.unlink()
                    raise ValueError("joined package digest does not match its manifest")
                os.replace(joined, output)
                job = self.store.update_archive_job(job)
            elif job.operation == "extract":
                source = Path(job.input_path).expanduser().resolve(strict=True)
                if not source.is_file():
                    raise ValueError("archive input must be a file")
                output = Path(job.output_directory).expanduser()
                if not output.is_absolute():
                    raise ValueError("archive output directory must be absolute")
                if output.exists() and output.is_symlink():
                    raise ValueError("archive output directory cannot be a symlink")
                validated = validate_directory(str(output), self.data_dir, create=True)
                if not validated.get("valid"):
                    raise ValueError(str(validated.get("error", "invalid archive output directory")))
                output = Path(str(validated["path"])).resolve()
                job = self.store.transition_archive_job(job.id, "extracting")
                self._archive_event("ArchiveProgress", job, reason="extraction_started")
                if job.password_ref:
                    password = self.secrets.resolve_operation(job.password_ref)
                    if not password:
                        raise NeedsUser("archive password is required", action="archive_password")
                    del password
                # Recovery fast-path: a crash after a fully promoted extraction
                # (worker sentinel present) but before source cleanup leaves the
                # payload in place. A crash mid-promote must NOT take this path:
                # the worker promote is resumable, so re-extract instead of
                # deleting sources for a partial payload.
                recovered_sentinel = completion_sentinel_path(output, job.id)
                if (
                    job.recovery_reason == "application_restart"
                    and recovered_sentinel.is_file()
                    and self._archive_produced_files(output, job)
                ):
                    result = {"output_directory": str(output), "bytes": 0, "files": 0}
                    fresh_extract = False
                else:
                    fresh_extract = True
                    result = await asyncio.to_thread(
                        self.archive_backend.extract, source, output, job.password_ref, job.policy, control,
                        job_id=job.id)
                if not isinstance(result, dict):
                    raise ValueError("archive worker returned an invalid result")
                reported_output = result.get("output_directory")
                if reported_output and Path(str(reported_output)).expanduser().resolve() != output:
                    raise ValueError("archive worker returned an unexpected output directory")
                observed_size = int(result.get("bytes", 0))
                observed_files = int(result.get("files", 0))
                policy = job.policy or {}
                if observed_size < 0 or observed_files < 0:
                    raise ValueError("archive worker returned negative output metrics")
                if observed_size > int(policy.get("archive_max_output_bytes", 500 * 1024**3)):
                    raise ValueError("archive output exceeds configured byte limit")
                if observed_files > int(policy.get("archive_max_file_count", 100_000)):
                    raise ValueError("archive output exceeds configured file-count limit")
                if not output.exists() or not output.is_dir():
                    raise ValueError("archive worker did not produce the approved output directory")
                job.output_directory = str(output)
                job.progress_bytes = observed_size
                job.observed_size = observed_size
                job = self.store.update_archive_job(job)
                self._archive_event("ArchiveProgress", job)
            else:
                observed_size = sum(path.stat().st_size for path in parts)
                job.progress_bytes = observed_size
                job.observed_size = observed_size
                job = self.store.update_archive_job(job)

            job = self.store.transition_archive_job(job.id, "verifying")
            self._archive_event("ArchiveVerifying", job)
            if job.operation != "extract" and job.expected_size is not None and job.observed_size != job.expected_size:
                raise ValueError("archive package integrity size mismatch")
            if job.expected_digest and job.observed_digest and job.observed_digest.casefold() != job.expected_digest.casefold():
                raise ValueError("archive package integrity digest mismatch")
            if job.operation == "extract":
                # Verify payload presence AND completeness before any source
                # deletion: the worker's self-reported bytes/files must be backed
                # by real, non-staging files on disk plus its completion sentinel.
                produced_files = self._archive_produced_files(output, job)
                if not produced_files:
                    raise ValueError("archive extraction produced no verified output files")
                produced_bytes = sum(path.stat().st_size for path in produced_files if path.is_file())
                if observed_size > 0 and produced_bytes < observed_size:
                    raise ValueError(
                        f"archive extraction payload is smaller than reported "
                        f"({produced_bytes} < {observed_size})")
                sentinel = completion_sentinel_path(output, job.id)
                if fresh_extract and not sentinel.is_file():
                    raise ValueError("archive worker completion sentinel is missing")
                # Durable cleanup intent: only after this point may sources be deleted.
                job = self.store.transition_archive_job(job.id, "cleanup_pending")
                self._archive_event("ArchiveCleanupPending", job)
                deleted_count = await self._cleanup_archive_sources(job)
                if deleted_count is None:
                    # Sources are still locked; stay in cleanup_pending so boot
                    # recovery retries the delete instead of reporting success.
                    self._archive_event("ArchiveProgress", job, reason="cleanup_blocked")
                    return
                telemetry_bus.record(
                    level="INFO",
                    subsystem="engine:archive",
                    message=f"[ARCHIVE_PARTS_CLEANED] Cleaned up {deleted_count} source archive files after extraction",
                    context={"job_id": job.id, "deleted_count": deleted_count},
                    tier="engine",
                )
                # The sentinel only exists to prove a crash happened AFTER a full
                # extract but BEFORE cleanup. Sources are now gone, so it has no
                # remaining job and would otherwise sit in the user's download
                # folder forever as litter next to their extracted game.
                try:
                    sentinel.unlink(missing_ok=True)
                except OSError as exc:
                    telemetry_bus.record(
                        level="DEBUG",
                        subsystem="engine:archive",
                        message=f"[ARCHIVE_SENTINEL_KEPT] Could not remove completion sentinel: {exc}",
                        context={"job_id": job.id, "sentinel": str(sentinel)},
                        tier="engine",
                    )
            else:
                deleted_count = 0
            job = self.store.transition_archive_job(job.id, "completed")
            self._archive_event("ArchiveCompleted", job)
            self._apply_archive_cleanup_result(job, deleted_count)
        except DownloadCanceled:
            current = self.store.get_archive_job(job_id)
            if current and current.state in {"running", "extracting", "verifying", "cleanup_pending"}:
                job = self.store.transition_archive_job(job_id, "canceled", error="archive job canceled")
                self._archive_event("ArchiveCanceled", job)
        except NeedsUser as exc:
            current = self.store.get_archive_job(job_id)
            if current and current.state in {"running", "extracting", "verifying", "cleanup_pending"}:
                job = self.store.transition_archive_job(job_id, "needs_user", error=str(exc), recovery_reason="password_required")
                self._archive_event("ArchiveNeedsUser", job, reason=getattr(exc, "action", "user_action"))
        except Exception as exc:
            current = self.store.get_archive_job(job_id)
            if current and current.state in {"running", "extracting", "verifying", "cleanup_pending"}:
                job = self.store.transition_archive_job(job_id, "failed", error=str(exc)[:500])
                self._archive_event("ArchiveFailed", job, reason=job.error)
            else:
                telemetry_bus.record(
                    level="WARN",
                    subsystem="engine:archive",
                    message="[ARCHIVE_FAILURE_NOT_TRANSITIONED] Archive job failed in a state that cannot transition",
                    context={"job_id": job_id, "state": current.state if current else None,
                             "error": str(exc)[:300]},
                )
        finally:
            if permit:
                self.resources.active_segments.release()
            self._archive_controls.pop(job_id, None)
            self._archive_inflight.discard(job_id)

    async def _download_external_item(self, download, item, destination, progress, control,
                                      task_id: str | None = None):
        """Apply the same provider/account/host admission to opaque backends."""
        provider = str(getattr(item, "provider", "") or "").lower()
        requested_width = self.resources.policy.provider_limits.get(
            provider, self.resources.policy.per_provider_transfers)
        host = await self.resources.prepare_segment_window(
            item.direct_url or item.source_url,
            requested_width,
        )
        await host.acquire()
        success = False
        try:
            async with self.resources.item_slot(item):
                await self.resources.acquire_request(item.direct_url or item.source_url)
                target_host = (urlsplit(item.direct_url or item.source_url).hostname or "unknown").lower()
                adaptive_key = (
                    f"{provider}:{(item.metadata or {}).get('account_ref') or '-'}:{target_host}"
                )
                adaptive_snapshot = next(
                    (row for row in self.resources.adaptive.snapshot()
                     if row.get("key") == adaptive_key),
                    None,
                )
                telemetry_bus.record(
                    level="INFO",
                    subsystem="engine:concurrency",
                    message="[TRANSFER_ADMISSION_GRANTED] Backend transport is starting",
                    context={
                        "task_id": task_id,
                        "provider": provider,
                        "host": target_host,
                        "requested_width": requested_width,
                        "provider_slot_width": self.resources.provider_slot_widths.get(provider),
                        "host_window": host.snapshot(),
                        "adaptive_permit": adaptive_snapshot,
                    },
                    tier="engine",
                )
                result = await asyncio.to_thread(download, item, destination, progress, control)
                success = True
                return result
        finally:
            await host.release(success)

    async def _enqueue_task(
        self,
        task_id: str,
        secrets: dict[str, str],
        control: _TaskControl,
        backend_name: str,
        group: str,
    ) -> None:
        if self._scheduler is None:
            raise RuntimeError("transfer scheduler is not ready")
        task = self.store.get(task_id)
        await self._scheduler.submit(
            group,
            lambda: self._run_task_async(task_id, secrets, control, backend_name),
            priority=-(task.priority if task else 0),
            dedupe_key=f"task:{task_id}",
        )

    def _requeue_task_later(
        self,
        task_id: str,
        secrets: dict[str, str],
        control: "_TaskControl",
        backend_name: str,
        group: str,
        delay: float = 0.0,
    ) -> None:
        """Re-dispatch a task without blocking the current scheduler worker.

        Backoff and requeue run on a detached coroutine so the worker slot is
        released immediately; awaiting ``_enqueue_task`` from inside a worker
        would self-deadlock the bounded pool (each retry consuming another slot).
        """
        async def _later() -> None:
            if task_id in self._deleted_task_ids:
                return
            effective_delay = max(0.0, delay)
            task = self.store.get(task_id)
            host = (urlsplit(task.source_url).hostname or "").lower() if task else ""
            if host:
                snap = concurrency_auditor.breaker_snapshot(host)
                remaining = float(snap.get("cooldown_remaining") or 0.0)
                if remaining > effective_delay:
                    telemetry_bus.record(
                        level="INFO",
                        subsystem="engine:transfer",
                        message="[RETRY_BACKOFF_EXTENDED] Retry delayed to cover host pressure cooldown",
                        context={"task_id": task_id, "host": host, "planned": effective_delay,
                                 "cooldown_remaining": remaining, "state": snap.get("state")},
                    )
                    effective_delay = min(remaining, 300.0)
            try:
                if effective_delay > 0:
                    await asyncio.sleep(effective_delay)
            except asyncio.CancelledError:
                return
            if control.cancel.is_set() or task_id in self._deleted_task_ids:
                return
            if control.pause.is_set():
                task = self.store.get(task_id)
                if task and task.state != "paused":
                    task.state, task.paused_reason = "paused", "Paused during retry backoff"
                    self.store.save(task)
                return
            try:
                await self._enqueue_task(task_id, secrets, control, backend_name, group)
            except Exception as exc:
                task = self.store.get(task_id)
                if task:
                    self._log_task(task, "error", "requeue",
                                   f"Failed to requeue task: {_redact_diagnostic(str(exc))}",
                                   {"error": _redact_diagnostic(str(exc))})

        self._futures[task_id] = asyncio.run_coroutine_threadsafe(_later(), self._loop)

    async def _run_transport(self, backend_name: str, task: DownloadTask, item, destination,
                             progress, control, route_profile):
        """Move one item's bytes through the named transport."""
        if backend_name == "rust":
            # The route profile is bound here rather than threaded through
            # _download_external_item, which stays a generic admission wrapper.
            transfer = functools.partial(self.rust_backend.download, route_profile=route_profile)
            outcome = await self._download_external_item(
                transfer, item, destination, progress, control, task.id)
            # Report the ranges the core actually used. The session log's
            # "streams_allocated" was the resolved-item count, which is
            # always 1 for a package part, so a single-connection transfer
            # and an 8-way segmented one looked identical in diagnostics.
            sess_logger = download_loggers.get(task.id)
            if sess_logger is not None:
                sess_logger.streams_count = max(
                    1, int(getattr(self.rust_backend, "last_segments", 1) or 1))
            return outcome
        return await self.custom_backend.download(item, destination, progress, control, route_profile)

    def _fallback_candidate(self, item, route_profile, primary: str) -> str | None:
        """Name a transport that could take this item if ``primary`` fails.

        Choosing a fallback must never be able to fail louder than the transfer
        it is reacting to: any problem here means no alternate, so the original
        transport error is what the caller sees.
        """
        try:
            backends = self._transfer_backends()
        except Exception:
            return None
        compatible = {}
        for name in backends:
            if name == primary:
                continue
            try:
                compatible[name] = self.backend_selector.select(
                    item, route_profile, backends, name).compatible
            except Exception:
                compatible[name] = False
        return transport_fallback.fallback_backend(primary, compatible)

    def _record_transport_attempt(self, task: DownloadTask, item, backend: str, outcome: str,
                                  reason: str = "", rescued_by: str | None = None) -> None:
        """One durable line per transport attempt, for the backend-share report."""
        telemetry_bus.record(
            level="INFO" if outcome != "failed" else "WARN",
            subsystem="engine:transport",
            message=f"[TRANSPORT_ATTEMPT] {backend} {outcome}: {item.display_name}",
            context={
                "task_id": task.id,
                "provider": getattr(item, "provider", None),
                "backend": backend,
                "outcome": outcome,
                "reason": reason,
                "rescued_by": rescued_by,
                "route_kind": (task.route_profile_id or "direct"),
            },
            tier="engine",
        )

    async def _transfer_with_fallback(self, task: DownloadTask, item, destination, progress,
                                      control, route_profile, backend_name: str):
        """Run the selected transport, retrying once on the other one.

        The Rust core carries almost all traffic, which leaves the Python
        transport exercised only by the test suite -- and tests never meet the
        servers that actually break a client. Retrying real failures on it keeps
        the second path honest against real traffic and produces the evidence
        for whether it still earns its place.

        The two transports fail differently on purpose: rustls refuses the
        legacy TLS that Python's stack accepts, and each has its own range and
        retry behaviour. That is what makes the second attempt worth making
        rather than a slower repeat of the first.
        """
        try:
            path = await self._run_transport(
                backend_name, task, item, destination, progress, control, route_profile)
        except Exception as exc:
            decision = transport_fallback.classify_failure(exc)
            alternate = self._fallback_candidate(item, route_profile, backend_name) if decision.should_fallback else None
            if alternate is None:
                self._record_transport_attempt(task, item, backend_name, "failed", decision.reason)
                raise
            self._record_transport_attempt(task, item, backend_name, "failed", decision.reason)
            self._log_task(task, "warn", "transport",
                           f"{backend_name} transport failed; retrying on {alternate}",
                           {"reason": decision.reason, "fallback_backend": alternate})
            self.events.emit("TaskTransportFallback", task.id, {
                "from_backend": backend_name,
                "to_backend": alternate,
                "reason": decision.reason,
            })
            try:
                path = await self._run_transport(
                    alternate, task, item, destination, progress, control, route_profile)
            except Exception as fallback_exc:
                self._record_transport_attempt(task, item, alternate, "failed",
                                               transport_fallback.classify_failure(fallback_exc).reason)
                # Surface the fallback failure, but keep the original cause
                # attached: the first failure is what explains the retry.
                raise fallback_exc from exc
            self._record_transport_attempt(task, item, alternate, "rescued",
                                           f"after {backend_name}: {decision.reason}",
                                           rescued_by=alternate)
            return path
        self._record_transport_attempt(task, item, backend_name, "completed")
        return path

    async def _download_item_with_refresh(self, task: DownloadTask, item_index: int, item, destination,
                                          progress, control, route_profile, backend_name: str, secrets,
                                          metrics=None):
        """Download once, refreshing only provider-classified expired URLs."""
        from .segment_budget import segment_budget

        is_multipart, package_key, _part_number = self._task_multipart_info(task)
        budget = segment_budget(getattr(item, "provider", ""), is_multipart=is_multipart)
        if budget is not None:
            item.metadata = {
                **(item.metadata or {}),
                "max_segments": budget.max_segments,
                "segment_budget_reason": budget.reason,
            }
            telemetry_bus.record(
                level="INFO",
                subsystem="engine:transfer",
                message="[TRANSFER_SEGMENT_BUDGET] Per-file connection budget selected",
                context={
                    "task_id": task.id,
                    "provider": item.provider,
                    "package_key": package_key,
                    "max_segments": budget.max_segments,
                    "reason": budget.reason,
                },
            )
        for attempt in range(2):
            try:
                return await self._transfer_with_fallback(
                    task, item, destination, progress, control, route_profile, backend_name)
            except Exception as exc:
                status = getattr(exc, "code", None) or getattr(exc, "status", None)
                text = str(exc).lower()
                expired = status in {401, 403, 410} and ("expired" in text or status in {403, 410})
                if attempt or not expired:
                    raise
                host = (urlsplit(item.direct_url or item.source_url).hostname or "").lower()
                cached = self.get_host_session(host, task.route_profile_id) if host else {}
                effective_secrets = {**cached, **secrets}
                refreshed = await asyncio.to_thread(self.plugins.refresh, item, "expired_url", effective_secrets, status)
                if not refreshed:
                    raise
                if metrics is not None:
                    metrics.record_refresh()
                replacement = next((candidate for candidate in refreshed if candidate.item_id == item.item_id), refreshed[0])
                replacement.metadata = {**replacement.metadata, "duplicate_strategy": task.duplicate_strategy}
                task.resolved[item_index] = replacement
                item = replacement
        raise AssertionError("unreachable")

    def _intake_analyze(self, raw_url: str) -> dict[str, Any]:
        """Classify a pasted link for the Add window (file, hoster, folder, page, ...)."""
        from dataclasses import asdict
        from .shortlinks import is_catalogued, load_catalog

        raw_url = raw_url.strip()
        if not raw_url:
            raise ValueError("Paste a link to check")
        url = raw_url if raw_url.lower().startswith("magnet:") else _normalize_input_url(raw_url)
        catalog = getattr(self, "_shortlink_catalog_cache", None)
        if catalog is None:
            catalog = self._shortlink_catalog_cache = load_catalog()

        def unwrap(target: str) -> tuple[str, list[dict[str, Any]]]:
            coro = self.shortlink_resolver.resolve_chain(target, None)
            if self._loop and self._loop.is_running():
                final_url, hops = asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=30)
            else:
                final_url, hops = asyncio.run(coro)
            return final_url, [asdict(hop) for hop in hops]

        return intake_analysis.analyze_link(
            url,
            inspect=self.plugins.inspect_url,
            provider_for=self.plugins.provider_for,
            is_shortlink=lambda target: is_catalogued(target, catalog),
            unwrap_shortlink=unwrap,
            probe=intake_analysis.probe_url,
            crawl=self.crawl_page_matrix,
        )

    def _favicons(self):
        """Lazily created so engines that never show icons never touch the disk."""
        cache = getattr(self, "_favicon_cache", None)
        if cache is None:
            from .favicons import FaviconCache
            cache = self._favicon_cache = FaviconCache(self.data_dir / "favicons")
        return cache

    def _adblock(self):
        """Lazily created; the first use compiles the lists in the core."""
        blocker = getattr(self, "_ad_blocker", None)
        if blocker is None:
            from .adblock import AdBlocker
            blocker = self._ad_blocker = AdBlocker(self.data_dir)
        return blocker

    def _wipe_explore_on_launch(self) -> None:
        """Explore starts empty each launch: earlier captures are marked dismissed
        (kept so a re-sent capture is still recognised) and pasted links removed.
        Downloads and History are untouched."""
        dismissed = 0
        for batch in self.store.list_capture_batches(500):
            if batch.get("state") not in {"dismissed", "imported"}:
                self.store.update_capture_batch(batch["batch_id"], state="dismissed", acknowledged=True)
                dismissed += 1
        links = self.store.list_links()
        for link in links:
            self.store.delete_link(link["id"])
        telemetry_bus.record(level="INFO", subsystem="engine:explore",
                             message=f"[EXPLORE_WIPED] {dismissed} captures dismissed, {len(links)} pasted links cleared on launch",
                             context={"dismissed": dismissed, "links": len(links)}, tier="engine")

    def _refresh_filter_lists_if_due(self) -> None:
        """Fetch the full lists in the background when enabled and a week old (or missing)."""
        network = self._ui_settings()["network"]
        if network.get("adblock", True) and network.get("adblockFullLists", True) and self._adblock().stale():
            threading.Thread(target=self._adblock().update, daemon=True, name="filter-lists").start()

    def crawl_page_matrix(self, url: str, force_headless: bool = False) -> dict[str, Any]:
        import re
        import urllib.request
        import urllib.parse
        from .dom_cleaner import DomCleaner
        from .element_scorer import ElementScorer

        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("URL must have http or https scheme")

        used_headless = False
        html_content = ""
        fetch_error = None

        if not force_headless:
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/128.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                    }
                )
                with route_http.urlopen(req, timeout=12.0) as resp:
                    raw_bytes = resp.read(2 * 1024 * 1024)
                    charset = "utf-8"
                    content_type = resp.headers.get("Content-Type", "")
                    if "charset=" in content_type.lower():
                        match = re.search(r"charset=([\w-]+)", content_type, re.I)
                        if match:
                            charset = match.group(1)
                    html_content = raw_bytes.decode(charset, errors="replace")
            except Exception as e:
                fetch_error = str(e)

        cleaner = DomCleaner(base_url=url)
        if html_content:
            try:
                cleaner.feed(html_content)
            except Exception:
                pass

        needs_headless = (
            force_headless or
            not html_content or
            len(cleaner.elements) == 0 or
            cleaner.has_countdown_timer
        )

        if needs_headless and self.flaresolverr and self.flaresolverr.is_available():
            try:
                timeout_ms = 45000
                if cleaner.countdown_seconds and cleaner.countdown_seconds > 0:
                    timeout_ms = max(45000, (cleaner.countdown_seconds + 5) * 1000)

                coro = self.flaresolverr.resolve(
                    url=url,
                    method="GET",
                    max_timeout_ms=timeout_ms,
                )
                if self._loop and self._loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(coro, self._loop)
                    resolution = future.result(timeout=float(timeout_ms) / 1000.0 + 5.0)
                else:
                    resolution = asyncio.run(coro)

                if resolution and resolution.response_html:
                    headless_cleaner = DomCleaner(base_url=url)
                    headless_cleaner.feed(resolution.response_html)
                    if len(headless_cleaner.elements) > 0 or not html_content:
                        cleaner = headless_cleaner
                        used_headless = True
            except Exception as ex:
                if not html_content:
                    raise RuntimeError(f"Failed to fetch page: static ({fetch_error}), headless ({ex})")

        if not html_content and not used_headless:
            raise RuntimeError(f"Failed to fetch page: {fetch_error}")

        if self._ui_settings()["network"].get("adblock", True):
            self._adblock().strip_page(cleaner, url)
        scored_elements = ElementScorer.score_all(cleaner.elements, source_url=url)
        self._favicons().remember_hint(url, cleaner.favicon_url)

        return {
            "url": url,
            "title": cleaner.page_title or parsed.netloc,
            "favicon": cleaner.favicon_url,
            "total_elements": len(scored_elements),
            "ads_stripped": len(cleaner.stripped_ads),
            "used_headless": used_headless,
            "has_countdown_timer": cleaner.has_countdown_timer,
            "elements": [elem.to_dict() for elem in scored_elements],
        }

    def _logs_query(self, params: dict[str, Any]) -> dict[str, Any]:
        since_id = params.get("since_id")
        min_level = params.get("min_level")
        subsystem = params.get("subsystem")
        limit = int(params.get("limit", 200))
        events = telemetry_bus.query(
            since_id=int(since_id) if since_id is not None else None,
            min_level=str(min_level) if min_level else None,
            subsystem=str(subsystem) if subsystem else None,
            limit=limit,
        )
        return {"events": events, "count": len(events)}

    def _logs_clear(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        telemetry_bus.clear()
        return {"cleared": True}

    def _logs_export(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        target = params.get("target_path")
        path = telemetry_bus.export_snapshot(target)
        return {"export_path": str(path)}

    def _log_event(self, params: dict[str, Any]) -> dict[str, Any]:
        level = str(params.get("level", "INFO"))
        subsystem = str(params.get("subsystem", "ui:general"))
        message = str(params.get("message", ""))
        context = params.get("context") if isinstance(params.get("context"), dict) else None
        error = params.get("error") if isinstance(params.get("error"), dict) else None
        duration_ms = float(params["duration_ms"]) if "duration_ms" in params and params["duration_ms"] is not None else None
        tier = str(params.get("tier", "ui"))
        seq_id = telemetry_bus.record(
            level=level,
            subsystem=subsystem,
            message=message,
            context=context,
            error=error,
            duration_ms=duration_ms,
            tier=tier,
        )
        return {"recorded": True, "seq_id": seq_id}


def serve(data_dir: str, *, input_stream: Any | None = None, output_stream: Any | None = None,

          service: EngineService | None = None, token: str | None = None) -> None:
    """Serve authenticated JSON-RPC over stdio using the same headless gate as HTTP."""
    owned_service = service is None
    service = service or EngineService(data_dir)
    if owned_service:
        # The app itself (not a test harness): refresh week-old filter lists in
        # the background, on the active route.
        service._refresh_filter_lists_if_due()
        service._wipe_explore_on_launch()
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    bootstrap_token = token or os.environ.get("TRANSFER_MANAGER_API_TOKEN")
    session: dict[str, Any] | None = None

    def token_from(request: dict[str, Any]) -> str:
        auth = request.get("auth")
        if isinstance(auth, dict):
            return str(auth.get("token") or auth.get("bearer") or "")
        return str(request.get("token") or "")

    def emit(response: dict[str, Any]) -> None:
        output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
        output_stream.flush()

    try:
        for line in input_stream:
            if not line.strip():
                continue
            request = json.loads(line)
            try:
                if session is None:
                    if request.get("method") == "hello":
                        params = request.get("params") or {}
                        if not isinstance(params, dict) or params.get("protocol_version", API_VERSION) != API_VERSION:
                            raise ValueError("unsupported stdio protocol version")
                        requested_client = str(params.get("client_id") or request.get("client_id") or "")
                        if not requested_client:
                            raise ValueError("stdio hello requires client_id")
                        auth_context = authenticate_headless_token(service, token_from({**request, **params}), bootstrap_token)
                        if auth_context is None or requested_client != str(auth_context["client_id"]):
                            raise PermissionError("invalid stdio credentials")
                        session = auth_context
                        result = {"type": "hello_ack", "protocol_version": API_VERSION,
                                  "client_id": auth_context["client_id"],
                                  "scopes": sorted(auth_context["scopes"])}
                    elif bootstrap_token is not None:
                        raise PermissionError("stdio hello is required before dispatch")
                    else:
                        result = service.dispatch(request["method"], request.get("params", {}))
                else:
                    if str(request.get("client_id") or "") != str(session["client_id"]):
                        raise PermissionError("stdio client identity does not match hello")
                    result = pre_dispatch(service, request, session)
                response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
            except Exception as exc:
                import traceback
                trace = traceback.format_exc()
                method_name = request.get("method") if isinstance(request, dict) else "unknown"
                print(f"[ENGINE ERROR] RPC '{method_name}' failed: {type(exc).__name__}: {exc}\n{trace}", file=sys.stderr, flush=True)
                response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {
                    "type": type(exc).__name__, "message": _redact_diagnostic(str(exc)), "traceback": trace}}
            emit(response)
    finally:
        if owned_service:
            service.close()


if __name__ == "__main__":
    serve(sys.argv[1] if len(sys.argv) > 1 else str(Path.cwd() / ".transfer-manager"))
