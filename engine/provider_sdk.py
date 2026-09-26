"""Fixture, replay, and signed-catalog helpers for schema-v2 providers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_SECRET_PARTS = ("authorization", "cookie", "token", "secret", "password", "signature", "credential")
PROVIDER_OPERATIONS = {
    "resolve", "enumerate", "extract", "extract_links", "metadata", "authenticate",
    "refresh", "health", "helper",
}
PROVIDER_CAPABILITY_OPERATIONS = {
    "crawl": "enumerate", "decrypter": "extract_links", "media": "metadata",
    "account": "authenticate", "refresh": "refresh", "helper": "helper",
}
_FORBIDDEN_ENGINE_KEYS = {
    "db", "database", "engine", "engine_state", "lifecycle", "task_state",
    "admission", "resource_manager", "scheduler", "lease", "queue",
}


class ProviderContractError(ValueError):
    """An untrusted provider request or result violated the public contract."""


@dataclass(frozen=True)
class ProviderOperationRequest:
    provider_id: str
    operation: str
    params: dict[str, Any]
    protocol_version: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "operation": self.operation,
            "params": redact(self.params),
            "protocol_version": self.protocol_version,
        }


@dataclass(frozen=True)
class ProviderOperationResult:
    provider_id: str
    operation: str
    value: Any
    protocol_version: int = 2
    outcome: str = "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "operation": self.operation,
            "value": redact(self.value),
            "protocol_version": self.protocol_version,
            "outcome": self.outcome,
        }


def _walk_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_ENGINE_KEYS:
                raise ProviderContractError(f"provider data cannot control engine field: {key}")
            _walk_forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            _walk_forbidden_keys(child)


def validate_operation_envelope(provider_id: str, operation: str, value: Any,
                                *, declared: set[str] | None = None) -> Any:
    if operation not in PROVIDER_OPERATIONS:
        raise ProviderContractError(f"unsupported provider operation: {operation}")
    if declared is not None and operation not in declared:
        raise ProviderContractError(f"provider operation is not declared: {operation}")
    _walk_forbidden_keys(value)
    if operation in {"resolve", "enumerate", "extract", "extract_links", "refresh"}:
        if not isinstance(value, list):
            raise ProviderContractError(f"{operation} result must be a list")
        for item in value:
            if not isinstance(item, dict):
                raise ProviderContractError(f"{operation} result items must be objects")
            required = {"provider", "source_url", "display_name"}
            if not required.issubset(item):
                raise ProviderContractError(f"{operation} result item missing required fields")
    elif operation in {"metadata", "authenticate", "health", "helper"} and not isinstance(value, dict):
        raise ProviderContractError(f"{operation} result must be an object")
    return value


def declared_operations(manifest: dict[str, Any], recipe_operations: set[str] | None = None) -> set[str]:
    hooks = manifest.get("hooks", {}) or {}
    declared = {name for name, hook in hooks.items() if hook}
    declared.update(str(name) for name in (manifest.get("operations", {}) or {}) if name)
    if recipe_operations:
        declared.update(recipe_operations)
    return declared


def replay_fixture(fixture: dict[str, Any], operation: str, params: dict[str, Any],
                   *, provider_id: str = "fixture", declared: set[str] | None = None) -> ProviderOperationResult:
    """Replay a redacted operation fixture without invoking network or plugin code."""
    if not isinstance(fixture, dict):
        raise ProviderContractError("fixture must be an object")
    _walk_forbidden_keys(params)
    validate_operation_envelope(provider_id, operation, {}, declared=declared) if operation not in {
        "resolve", "enumerate", "extract", "extract_links", "refresh"
    } else None
    cases = fixture.get("operations", fixture)
    entry = cases.get(operation) if isinstance(cases, dict) else None
    if not isinstance(entry, dict):
        raise ProviderContractError(f"fixture has no operation: {operation}")
    expected = entry.get("request", {})
    if expected and expected != redact(params):
        raise ProviderContractError(f"fixture request mismatch for {operation}")
    value = entry.get("result", entry.get("response"))
    validate_operation_envelope(provider_id, operation, value, declared=declared)
    return ProviderOperationResult(provider_id, operation, redact(value))


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): ("[redacted]" if any(part in str(key).lower() for part in _SECRET_PARTS) else redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(value)
        query = urllib.parse.parse_qs(parsed.query)
        if any(key.lower() in {"token", "sig", "signature", "expires", "x-amz-signature", "access_token"} for key in query):
            return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return value


@dataclass
class RecordedExchange:
    request: dict[str, Any]
    response: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {"request": redact(self.request), "response": redact(self.response), "created_at": self.created_at}


class ReplayTransport:
    """Deterministic request recorder/replayer compatible with RecipeRuntime."""
    def __init__(self, exchanges: list[dict[str, Any]] | None = None) -> None:
        self.exchanges = [RecordedExchange(item["request"], item["response"], item.get("created_at", 0)) for item in (exchanges or [])]
        self.recording = True

    def __call__(self, request: Any, _timeout: float, _redirects: int) -> Any:
        key = getattr(request, "full_url", str(request))
        for exchange in self.exchanges:
            if exchange.request.get("url") == key:
                response = exchange.response
                # The runtime accepts its _Response object; callers can also
                # use ``response_factory`` for a custom HTTP abstraction.
                return response["value"] if "value" in response else response
        raise LookupError(f"no replay fixture for {key}")

    def record(self, request: Any, response: Any) -> None:
        self.exchanges.append(RecordedExchange(
            {"method": getattr(request, "method", "GET"), "url": getattr(request, "full_url", str(request)),
             "headers": dict(getattr(request, "headers", {}) or {})},
            {"value": response}, time.time()))

    def dump(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps([exchange.to_dict() for exchange in self.exchanges], indent=2), encoding="utf-8")


def generate_fixture(request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    return RecordedExchange(redact(request), redact(response), time.time()).to_dict()


def generate_fixture_bundle(exchanges: list[dict[str, Any]], provider_id: str | None = None) -> dict[str, Any]:
    """Create a redacted, replayable fixture bundle from recorded exchanges."""
    return {"schema_version": 1, "provider_id": provider_id,
            "exchanges": [generate_fixture(item.get("request", {}), item.get("response", {})) for item in exchanges]}


def run_contract_suite(provider_dir: str | Path, *, checker: Callable[[Path], dict[str, Any]] | None = None) -> dict[str, Any]:
    path = Path(provider_dir).resolve()
    if not (path / "manifest.jsonc").is_file():
        raise ValueError("provider fixture directory has no manifest.jsonc")
    result = checker(path) if checker else {"valid": True, "provider": path.name}
    result.setdefault("provider", path.name)
    result.setdefault("valid", True)
    return result


def sign_catalog(catalog: dict[str, Any], secret: str) -> dict[str, Any]:
    payload = json.dumps(redact(catalog), sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return {"payload": base64.urlsafe_b64encode(payload).decode(), "signature": signature, "algorithm": "HMAC-SHA256"}


def verify_catalog(signed: dict[str, Any], secret: str) -> dict[str, Any]:
    payload = base64.urlsafe_b64decode(str(signed["payload"]).encode())
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(signed.get("signature", ""))):
        raise ValueError("provider catalog signature verification failed")
    return json.loads(payload.decode())


def catalog_entry_digest(entry: dict[str, Any]) -> str:
    payload = json.dumps(redact(entry), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def validate_catalog_entry(entry: dict[str, Any], *, allow_hosts: set[str] | None = None,
                           allow_operations: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
        raise ProviderContractError("catalog entry requires a provider id")
    hosts = entry.get("hosts", [])
    operations = entry.get("operations", entry.get("capabilities", {}))
    operation_names = set(operations if isinstance(operations, list) else operations)
    if allow_hosts is not None and not set(hosts).issubset(allow_hosts):
        raise ProviderContractError("catalog host is not allowlisted")
    if allow_operations is not None and not operation_names.issubset(allow_operations):
        raise ProviderContractError("catalog operation is not allowlisted")
    digest = entry.get("digest")
    if not digest:
        raise ProviderContractError("catalog entry is missing a digest")
    if not hmac.compare_digest(str(digest), catalog_entry_digest({k: v for k, v in entry.items() if k != "digest"})):
        raise ProviderContractError("catalog entry digest verification failed")
    return entry


def validate_signed_catalog(signed: dict[str, Any], secret: str, *,
                           allow_hosts: set[str] | None = None,
                           allow_operations: set[str] | None = None) -> dict[str, Any]:
    """Verify the catalog envelope and every provider admission entry."""
    try:
        catalog = verify_catalog(signed, secret)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderContractError("signed provider catalog verification failed") from exc
    providers = catalog.get("providers") if isinstance(catalog, dict) else None
    if not isinstance(providers, list) or not providers:
        raise ProviderContractError("signed provider catalog requires providers")
    for entry in providers:
        validate_catalog_entry(entry, allow_hosts=allow_hosts, allow_operations=allow_operations)
    return catalog
