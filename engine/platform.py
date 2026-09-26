"""Small, dependency-free platform contracts shared by local and future clients."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


API_VERSION = "v1"


@dataclass(slots=True)
class EventEnvelope:
    event_id: str
    event_type: str
    resource_id: str | None
    revision: int | None
    correlation_id: str
    created_at: float
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, event_type: str, resource_id: str | None, payload: dict[str, Any],
               revision: int | None = None, correlation_id: str | None = None) -> "EventEnvelope":
        return cls(str(uuid4()), event_type, resource_id, revision, correlation_id or str(uuid4()), time.time(), payload)

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "event_type": self.event_type,
                "resource_id": self.resource_id, "revision": self.revision,
                "correlation_id": self.correlation_id, "created_at": self.created_at,
                "payload": self.payload}


@dataclass(slots=True)
class WorkflowJob:
    id: str
    kind: str
    state: str = "queued"
    steps: list[dict[str, Any]] = field(default_factory=list)
    progress: float = 0.0
    error: str | None = None
    revision: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "state": self.state, "steps": self.steps,
                "progress": self.progress, "error": self.error, "revision": self.revision,
                "created_at": self.created_at, "updated_at": self.updated_at}


def request_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def api_description() -> dict[str, Any]:
    return {"name": "transfer-manager", "version": API_VERSION,
            "resource_types": ["tasks", "accounts", "links", "collections", "media", "events", "workflows", "providers"],
            "features": {"idempotency": True, "optimistic_concurrency": True,
                          "loopback_default": True, "secret_refs_only": True}}


def worker_hello(worker_id: str, capabilities: list[str], protocol_version: str = "v1") -> dict[str, Any]:
    if not worker_id or not isinstance(capabilities, list):
        raise ValueError("worker_id and capabilities are required")
    return {"worker_id": worker_id, "protocol_version": protocol_version,
            "capabilities": sorted({str(item) for item in capabilities}), "state": "available"}


@dataclass(slots=True)
class JsonRpcEnvelope:
    method: str
    id: str | int | None = None
    jsonrpc: str = "2.0"
    params: dict[str, Any] = field(default_factory=dict)
    client_id: str | None = None
    auth: dict[str, Any] = field(default_factory=dict)
    scopes: list[str] = field(default_factory=list)
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
            "id": self.id,
            "method": self.method,
            "params": self.params,
        }
        if self.client_id is not None:
            result["client_id"] = self.client_id
        if self.auth:
            result["auth"] = self.auth
        if self.scopes:
            result["scopes"] = self.scopes
        if self.idempotency_key is not None:
            result["idempotency_key"] = self.idempotency_key
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JsonRpcEnvelope":
        if not isinstance(data, dict):
            raise ValueError("request envelope must be an object")
        auth = data.get("auth")
        auth_dict = auth if isinstance(auth, dict) else ({"token": str(auth)} if auth else {})
        return cls(
            method=str(data.get("method") or ""),
            id=data.get("id"),
            jsonrpc=str(data.get("jsonrpc", "2.0")),
            params=dict(data.get("params") or {}) if isinstance(data.get("params"), dict) else {},
            client_id=str(data["client_id"]) if data.get("client_id") is not None else None,
            auth=auth_dict,
            scopes=list(data.get("scopes") or []),
            idempotency_key=str(data["idempotency_key"]) if data.get("idempotency_key") is not None else None,
        )


JsonRpcRequest = JsonRpcEnvelope


@dataclass(slots=True)
class JsonRpcResponse:
    id: str | int | None = None
    jsonrpc: str = "2.0"
    result: Any = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.error is not None:
            return {"jsonrpc": self.jsonrpc, "id": self.id, "error": self.error}
        return {"jsonrpc": self.jsonrpc, "id": self.id, "result": self.result}

