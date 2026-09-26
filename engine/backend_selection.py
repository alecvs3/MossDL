"""Capability-driven transfer backend selection.

Selection is deliberately a pure engine policy.  It does not admit work,
change task state, or invoke a backend.  Adapters remain responsible only for
transferring a resolved item through their existing compatibility methods.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from .transfer_contracts import BackendCapabilities


_BACKEND_PREFERENCE = {"rust": 300, "custom": 100}
_KNOWN_BACKENDS = tuple(_BACKEND_PREFERENCE)


def _route_kind(route_profile: Mapping[str, Any] | None) -> str:
    return str((route_profile or {}).get("kind", "direct") or "direct").lower()


def _runtime_capabilities(name: str, backend: Any) -> BackendCapabilities:
    """Return declared capabilities annotated with a live availability probe."""
    declared = backend if isinstance(backend, BackendCapabilities) else backend.capabilities()
    if not isinstance(declared, BackendCapabilities):
        raise TypeError(f"backend {name!r} returned invalid capabilities")
    declared = replace(declared, name=name or declared.name)
    probe = getattr(backend, "available", None)
    if callable(probe):
        try:
            available = bool(probe())
        except Exception as exc:  # an unhealthy optional adapter is unavailable
            return replace(declared, available=False, availability_reason=f"availability check failed: {exc}")
        if not available:
            return replace(declared, available=False,
                           availability_reason=declared.availability_reason or f"{name} adapter is unavailable")
    return declared


def _item_requirements(item: Any) -> dict[str, bool]:
    metadata = getattr(item, "metadata", {}) or {}
    postprocess = bool(getattr(item, "postprocess", {}) or {})
    media = bool(metadata.get("media_plan")) or str(getattr(item, "provider", "")) == "media"
    headers = getattr(item, "headers", {}) or {}
    cookies = getattr(item, "cookies", {}) or {}
    sensitive_headers = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
    requires_ranges = bool(metadata.get("requires_ranges", False))
    requires_dynamic = bool(metadata.get("requires_dynamic_ranges", False))
    return {
        "resume": True,
        "pause": True,
        "cancel": True,
        "refresh": bool(metadata.get("requires_refresh", True)),
        "integrity": True,
        "ranges": requires_ranges,
        "dynamic_ranges": requires_dynamic,
        "postprocess": postprocess,
        "streaming_transform": bool(metadata.get("requires_streaming_transform", False)),
        "media_segments": media,
        "operation_scoped_headers": bool(cookies) or any(str(key).lower() in sensitive_headers for key in headers),
    }


def _capability_failures(capabilities: BackendCapabilities, item: Any,
                         route_profile: Mapping[str, Any] | None) -> list[str]:
    requirements = _item_requirements(item)
    failures: list[str] = []
    if not capabilities.available:
        failures.append(capabilities.availability_reason or f"{capabilities.name} adapter is unavailable")
    route = _route_kind(route_profile)
    if route not in capabilities.supported_route_kinds:
        failures.append(f"route kind {route!r} is not supported")
    fields = {
        "resume": "supports_resume",
        "pause": "supports_pause",
        "cancel": "supports_cancel",
        "refresh": "supports_refresh",
        "integrity": "supports_integrity",
        "ranges": "supports_ranges",
        "dynamic_ranges": "supports_dynamic_ranges",
        "postprocess": "supports_postprocess",
        "streaming_transform": "supports_streaming_transform",
        "media_segments": "supports_media_segments",
        "operation_scoped_headers": "supports_operation_scoped_headers",
    }
    for requirement, field in fields.items():
        if requirements[requirement] and not bool(getattr(capabilities, field)):
            failures.append(f"requires {requirement.replace('_', ' ')} capability")
    return failures


@dataclass(frozen=True, slots=True)
class BackendSelection:
    """Auditable result of a backend policy decision."""

    backend: str | None
    capabilities: BackendCapabilities | None
    compatible: bool
    available: bool
    requested_backend: str | None = None
    automatic: bool = True
    reason: str = ""
    diagnostics: tuple[str, ...] = ()

    @property
    def backend_name(self) -> str | None:
        return self.backend

    @property
    def selected_backend(self) -> str | None:
        return self.backend

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "selected_backend": self.backend,
            "compatible": self.compatible,
            "available": self.available,
            "requested_backend": self.requested_backend,
            "automatic": self.automatic,
            "reason": self.reason,
            "diagnostics": list(self.diagnostics),
            "capabilities": self.capabilities.to_dict() if self.capabilities else None,
        }


class BackendSelector:
    """Select the fastest currently available backend satisfying all needs."""

    def __init__(self, preference: Mapping[str, int] | None = None) -> None:
        self.preference = dict(preference or _BACKEND_PREFERENCE)

    @staticmethod
    def _normalize_backends(available_backends: Mapping[str, Any] | Iterable[Any]) -> dict[str, Any]:
        if isinstance(available_backends, Mapping):
            return {str(name): backend for name, backend in available_backends.items()}
        return {str(getattr(backend.capabilities(), "name")): backend for backend in available_backends}

    def select(self, item: Any, route_profile: Mapping[str, Any] | None,
               available_backends: Mapping[str, Any] | Iterable[Any],
               requested_backend: str | None = None) -> BackendSelection:
        backends = self._normalize_backends(available_backends)
        requested = str(requested_backend).strip().lower() if requested_backend is not None else None
        if requested:
            backend = backends.get(requested)
            if backend is None:
                return BackendSelection(None, None, False, False, requested, False,
                                        f"backend override {requested!r} is unknown; choose one of {sorted(backends)}")
            capabilities = _runtime_capabilities(requested, backend)
            failures = _capability_failures(capabilities, item, route_profile)
            if failures:
                return BackendSelection(requested, capabilities, False, capabilities.available, requested, False,
                                        f"backend override {requested!r} is incompatible: {'; '.join(failures)}",
                                        tuple(failures))
            return BackendSelection(requested, capabilities, True, True, requested, False,
                                    f"backend override {requested!r} accepted after capability validation")

        diagnostics: list[str] = []
        candidates: list[tuple[int, str, BackendCapabilities]] = []
        for name, backend in backends.items():
            capabilities = _runtime_capabilities(name, backend)
            failures = _capability_failures(capabilities, item, route_profile)
            if failures:
                diagnostics.append(f"{name}: {'; '.join(failures)}")
                continue
            candidates.append((self.preference.get(name, 0), name, capabilities))
        if not candidates:
            return BackendSelection(None, None, False, False, None, True,
                                    "no available backend satisfies the resolved item and route capabilities",
                                    tuple(diagnostics))
        _, selected, capabilities = max(candidates, key=lambda value: (value[0], value[1]))
        return BackendSelection(selected, capabilities, True, True, None, True,
                                f"automatically selected {selected} as the highest-ranked compatible backend",
                                tuple(diagnostics))

    def select_for_items(self, items: Iterable[Any], route_profile: Mapping[str, Any] | None,
                         available_backends: Mapping[str, Any] | Iterable[Any],
                         requested_backend: str | None = None,
                         package_backend: str | None = None) -> BackendSelection:
        """Select one backend safe for every item in a task package."""
        item_list = list(items)
        if not item_list:
            return BackendSelection(None, None, False, False, requested_backend, requested_backend is None,
                                    "cannot select a backend for an empty resolved item list")
        backends = self._normalize_backends(available_backends)
        requested = str(requested_backend).strip().lower() if requested_backend is not None else None
        pkg_backend = str(package_backend).strip().lower() if package_backend is not None else None

        # If a package backend affinity is requested (e.g. 'rust' for multipart packages),
        # verify whether it satisfies all items before evaluating individual overrides or fallbacks.
        if pkg_backend and pkg_backend in backends:
            pkg_caps = _runtime_capabilities(pkg_backend, backends[pkg_backend])
            pkg_failures = []
            for index, item in enumerate(item_list):
                pkg_failures.extend(f"item {index}: {failure}" for failure in _capability_failures(pkg_caps, item, route_profile))
            if not pkg_failures:
                return BackendSelection(pkg_backend, pkg_caps, True, True, requested, not bool(requested),
                                        f"package backend affinity {pkg_backend!r} satisfies all resolved items")

        candidates = [requested] if requested else sorted(backends, key=lambda name: (self.preference.get(name, 0), name), reverse=True)
        diagnostics: list[str] = []
        for name in candidates:
            backend = backends.get(name)
            if backend is None:
                diagnostics.append(f"{name}: adapter is not registered")
                continue
            capabilities = _runtime_capabilities(name, backend)
            failures = []
            for index, item in enumerate(item_list):
                failures.extend(f"item {index}: {failure}" for failure in _capability_failures(capabilities, item, route_profile))
            if not failures:
                mode = "override" if requested else "automatic"
                return BackendSelection(name, capabilities, True, True, requested, not requested,
                                        f"{mode} backend {name!r} satisfies all resolved items",
                                        tuple(diagnostics))
            diagnostics.append(f"{name}: {'; '.join(failures)}")
            if requested:
                return BackendSelection(name, capabilities, False, capabilities.available, requested, False,
                                        f"backend override {name!r} is incompatible: {'; '.join(failures)}",
                                        tuple(failures))
        return BackendSelection(None, None, False, False, requested, not requested,
                                "no available backend satisfies every resolved item and route capability",
                                tuple(diagnostics))

    def explain(self, item: Any, route_profile: Mapping[str, Any] | None,
                available_backends: Mapping[str, Any] | Iterable[Any],
                requested_backend: str | None = None) -> dict[str, Any]:
        return self.select(item, route_profile, available_backends, requested_backend).to_dict()


def capability_snapshot(available_backends: Mapping[str, Any] | Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Expose declared capabilities plus explicit runtime availability."""
    backends = BackendSelector._normalize_backends(available_backends)
    result: dict[str, dict[str, Any]] = {}
    for name, backend in backends.items():
        result[name] = _runtime_capabilities(name, backend).to_dict()
    return result


__all__ = ["BackendSelector", "BackendSelection", "capability_snapshot"]
