from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from typing import Any, ClassVar, Literal
from uuid import uuid4

TaskState = Literal[
    "queued", "pending_probe", "resolving", "preflight", "downloading", "verifying",
    "postprocessing", "needs_user", "paused", "retrying", "completed",
    "failed", "canceled",
]


@dataclass(slots=True)
class ResolvedItem:
    provider: str
    source_url: str
    display_name: str
    relative_path: str = ""
    size: int | None = None
    direct_url: str | None = None
    expires_at: float | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    checksum: str | None = None
    postprocess: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    item_id: str | None = None
    parent_id: str | None = None
    page: int | None = None
    cursor: str | None = None
    folder_path: str = ""
    package_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DownloadTask:
    source_url: str
    destination: str
    display_name: str | None = None
    password_ref: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    provider: str | None = None
    state: TaskState = "queued"
    size: int | None = None
    completed_bytes: int = 0
    error: str | None = None
    retry_count: int = 0
    resolved: list[ResolvedItem] = field(default_factory=list)
    route_profile_id: str | None = None
    alternate_urls: list[dict[str, Any]] = field(default_factory=list)
    priority: int = 0
    queue_id: str = "default"
    queue_order: int = 0
    scheduled_at: float | None = None
    paused_reason: str | None = None
    category: str | None = None
    duplicate_strategy: str = "skip"
    source_fingerprint: str | None = None
    finished_at: float | None = None
    user_action: str | None = None
    user_challenge: dict[str, Any] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict)
    referrer: str | None = None
    browser_context: dict[str, Any] = field(default_factory=dict)
    selected_item_ids: list[str] | None = None
    account_ref: str | None = None
    revision: int = 0
    lease_id: str | None = None
    heartbeat_at: float | None = None
    recovery_reason: str | None = None
    integrity: dict[str, Any] = field(default_factory=dict)
    folder_path: str | None = None
    package_key: str | None = None
    package_part_number: int | None = None
    package_part_count: int | None = None
    package_leader_id: str | None = None
    speed_bytes_per_second: float = 0.0
    average_speed_bytes_per_second: float = 0.0
    eta_seconds: float | None = None
    backend: str | None = None
    attempt_count: int = 0
    started_at: float | None = None
    telemetry_updated_at: float | None = None
    integrity_state: str = "unverified"
    stage: str | None = None
    stage_detail: dict[str, Any] = field(default_factory=dict)
    stage_entered_at: float | None = None
    stage_history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        # A task is a public resource.  Direct/signed URLs and operation-scoped
        # request material are intentionally not part of its public shape.
        value["password_ref"] = None
        value["request_headers"] = {}
        value["browser_context"] = {
            key: item for key, item in self.browser_context.items()
            if str(key).lower() in {
                "capture_source", "session_ref", "account_ref", "page_url",
                "diagnostic_resolution_only",
            }
        }
        value["resolved"] = []
        for item in self.resolved:
            public = item.to_dict()
            public["direct_url"], public["headers"], public["cookies"] = None, {}, {}
            value["resolved"].append(public)
        return value


@dataclass(slots=True)
class ProviderCapabilities:
    max_concurrency: int = 2
    supports_refresh: bool = True
    supports_ranges: bool = True
    supports_pause_resume: bool = True
    supports_folder_selection: bool = False
    requires_browser_handoff: bool = False
    plugin_limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DownloadPlan:
    task_id: str
    provider: str | None
    items: list[ResolvedItem]
    destination: str
    expected_bytes: int = 0
    policy_snapshot: dict[str, Any] = field(default_factory=dict)
    credentials_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["items"] = [item.to_dict() for item in self.items]
        return value


@dataclass(slots=True)
class TransferLease:
    lease_id: str
    task_id: str
    host: str
    provider: str
    account_ref: str | None = None
    segment_count: int = 0
    expires_at: float = 0.0
    released_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ArchiveJob:
    id: str
    task_id: str
    input_path: str
    output_directory: str
    format: str | None = None
    password_ref: str | None = None
    policy: dict[str, Any] = field(default_factory=dict)
    state: str = "queued"
    progress_bytes: int = 0
    error: str | None = None
    package_key: str | None = None
    operation: str = "join_verify"
    part_manifest: list[dict[str, Any]] = field(default_factory=list)
    expected_size: int | None = None
    observed_size: int | None = None
    expected_digest: str | None = None
    observed_digest: str | None = None
    attempt: int = 0
    recovery_reason: str | None = None
    revision: int = 0
    staging_path: str | None = None

    LEGAL_TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "queued": {"running", "canceled"},
        "running": {"extracting", "verifying", "needs_user", "failed", "canceled"},

        "extracting": {"verifying", "needs_user", "failed", "canceled"},
        "verifying": {"cleanup_pending", "completed", "failed", "canceled"},
        "cleanup_pending": {"completed", "failed", "canceled"},
        "needs_user": {"queued", "canceled"},
        "failed": {"queued", "canceled"},
        "canceled": {"queued"},
        "completed": set(),
    }

    def can_transition(self, state: str) -> bool:
        return state == self.state or state in self.LEGAL_TRANSITIONS.get(self.state, set())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RouteProfile:
    id: str
    kind: str = "direct"
    endpoint: str | None = None
    credential_ref: str | None = None
    region: str | None = None
    healthcheck_url: str | None = None
    enabled: bool = True
    # Appended: "core" runs a WireGuard route inside the transfer core (its
    # .conf in the secret store); "system" leaves it to the WireGuard app.
    tunnel: str | None = None
    # Appended: for account-based providers the secret holds the device key and
    # each location names its server's public key here.
    peer_public_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RouteHealth:
    profile_id: str
    healthy: bool
    checked_at: float
    public_ip: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RouteAttempt:
    task_id: str
    profile_id: str
    outcome: str
    reason: str | None = None
    started_at: float = 0.0
    ended_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DownloadQueue:
    id: str
    name: str
    enabled: bool = True
    paused: bool = False
    max_active: int | None = None
    start_hour: int | None = None
    end_hour: int | None = None
    created_at: float | None = None
    updated_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResolutionContext:
    """Provider-neutral context for resolving a source more than once.

    Direct URLs and bearer material deliberately do not belong in this object
    when it is persisted.  ``source_url`` and the opaque references are the
    durable identity; a broker may attach an in-memory resolved URL at runtime.
    """

    source_url: str
    provider_id: str | None = None
    browser_session_ref: str | None = None
    account_ref: str | None = None
    expires_at: float | None = None
    refresh_policy: str = "on_expiry"
    item_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CaptureCandidate:
    """A browser-observed request that can be replayed by the downloader."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    referrer: str | None = None
    credential_ref: str | None = None
    mime: str | None = None
    filename: str | None = None
    size: int | None = None
    confidence: float = 0.0
    source: str = "browser"
    request_id: str | None = None
    canonical_source: str | None = None
    source_fingerprint: str | None = None
    page_url: str | None = None
    page_origin: str | None = None
    session_ref: str | None = None
    ranking_inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CaptureBatch:
    """Durable review state for a redacted browser candidate batch."""

    batch_id: str
    request_id: str
    protocol_version: str
    extension_origin: str
    page_origin: str
    page_url: str | None
    session_ref: str | None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    state: str = "pending"
    acknowledged: bool = False
    imported_task_ids: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"batch_id": self.batch_id, "request_id": self.request_id,
                "protocol_version": self.protocol_version, "extension_origin": self.extension_origin,
                "page_origin": self.page_origin, "page_url": self.page_url,
                "session_ref": self.session_ref, "candidates": self.candidates,
                "state": self.state, "acknowledged": self.acknowledged,
                "imported_task_ids": self.imported_task_ids, "created_at": self.created_at,
                "updated_at": self.updated_at}


@dataclass(slots=True)
class MediaSegment:
    index: int
    url: str
    start: int | None = None
    end: int | None = None
    byte_range: str | None = None
    duration: float | None = None
    initialization: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MediaPlan:
    kind: Literal["hls", "dash"]
    manifest_url: str
    segments: list[MediaSegment] = field(default_factory=list)
    variants: list[dict[str, Any]] = field(default_factory=list)
    selected_variant: dict[str, Any] | None = None
    encrypted: bool = False
    output_format: str | None = None
    total_duration: float | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["segments"] = [segment.to_dict() for segment in self.segments]
        return value


@dataclass(slots=True)
class CollectionItem:
    stable_id: str
    source_url: str
    display_name: str
    relative_path: str = ""
    size: int | None = None
    selected: bool = False
    status: str = "pending"
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    depth: int = 0
    page: int = 0
    cursor: str | None = None
    folder_path: str = ""
    package_path: str = ""
    outcome: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CollectionPlan:
    id: str
    source_url: str
    provider_id: str | None = None
    items: list[CollectionItem] = field(default_factory=list)
    cursor: str | None = None
    page: int = 0
    has_more: bool = False
    max_pages: int = 100
    max_items: int = 10_000
    state: str = "active"
    outcome: str | None = None
    graph_revision: int = 0
    max_depth: int = 8
    nodes: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["items"] = [item.to_dict() for item in self.items]
        return value


@dataclass(slots=True)
class ProviderHealthSnapshot:
    provider_id: str
    success_rate: float = 0.0
    total_attempts: int = 0
    successes: int = 0
    failure_categories: dict[str, int] = field(default_factory=dict)
    last_known_version: str | None = None
    quarantined: bool = False
    last_error: str | None = None
    checked_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
