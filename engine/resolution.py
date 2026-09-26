"""Short-lived resolution/session broker with durable source identity only."""

from __future__ import annotations

import inspect
import time
from typing import Any, Callable

from .models import ResolutionContext, ResolvedItem


def public_item(item: ResolvedItem) -> dict[str, Any]:
    value = item.to_dict()
    value["direct_url"], value["headers"], value["cookies"] = None, {}, {}
    return value


class ResolutionBroker:
    def __init__(self, store: Any | None = None) -> None:
        self.store = store
        self._active: dict[tuple[str, str | None], list[ResolvedItem]] = {}

    @staticmethod
    def _invoke(resolver: Callable[..., list[ResolvedItem]], source: str,
                credentials: dict[str, str] | None = None) -> list[ResolvedItem]:
        """Support legacy one-argument providers and credential-aware ones."""
        try:
            accepts_credentials = len(inspect.signature(resolver).parameters) >= 2
        except (TypeError, ValueError):
            accepts_credentials = False
        return resolver(source, credentials or {}) if accepts_credentials else resolver(source)

    @staticmethod
    def _needs_refresh(exc: BaseException) -> bool:
        category = str(getattr(exc, "category", "")).lower()
        return category in {"login_required", "expired_source", "authentication", "refresh_required"} or \
            isinstance(exc, KeyError) and "credential" in str(exc).lower()

    def resolve(self, context: ResolutionContext, resolver: Callable[..., list[ResolvedItem]],
                *, credentials: dict[str, str] | None = None,
                refresh: Callable[..., Any] | None = None,
                refresh_reason: str = "login_required",
                on_refresh: Callable[[str], None] | None = None) -> list[ResolvedItem]:
        try:
            items = self._invoke(resolver, context.source_url, credentials)
        except Exception as exc:
            if refresh is None or not self._needs_refresh(exc):
                raise
            refreshed = self._invoke(refresh, context.source_url, credentials)
            if refreshed is False or refreshed is None:
                raise
            if on_refresh:
                on_refresh(refresh_reason)
            items = self._invoke(resolver, context.source_url, credentials)
        self._active[(context.source_url, context.item_id)] = items
        if self.store and hasattr(self.store, "save_resolution_cache"):
            self.store.save_resolution_cache(context, items)
        return items

    def current(self, context: ResolutionContext) -> list[ResolvedItem]:
        value = self._active.get((context.source_url, context.item_id))
        if value is not None:
            return list(value)
        if self.store and hasattr(self.store, "get_resolution_cache"):
            cached = self.store.get_resolution_cache(context.source_url, context.item_id)
            if cached:
                raw_items = (cached.get("metadata") or {}).get("items", [])
                result = [ResolvedItem(provider=item.get("provider", "generic"), source_url=item.get("source_url", context.source_url),
                                        display_name=item.get("display_name", "download"), relative_path=item.get("relative_path", ""),
                                        size=item.get("size"), checksum=item.get("checksum"), metadata=item.get("metadata", {}),
                                        item_id=item.get("item_id")) for item in raw_items]
                self._active[(context.source_url, context.item_id)] = result
                return result
        return []

    def refresh_if_needed(self, context: ResolutionContext, resolver: Callable[..., list[ResolvedItem]],
                          now: float | None = None, *, credentials: dict[str, str] | None = None,
                          refresh: Callable[..., Any] | None = None,
                          on_refresh: Callable[[str], None] | None = None) -> list[ResolvedItem]:
        items = self.current(context)
        moment = now if now is not None else time.time()
        if items and all(item.expires_at is None or item.expires_at > moment for item in items):
            return items
        if refresh is not None:
            refreshed = self._invoke(refresh, context.source_url, credentials)
            if refreshed is not False and refreshed is not None:
                if on_refresh:
                    on_refresh("expired_source")
                return self.resolve(context, resolver, credentials=credentials)
        return self.resolve(context, resolver, credentials=credentials)

    def resolve_with_account(self, context: ResolutionContext, resolver: Callable[..., list[ResolvedItem]],
                             credential_resolver: Callable[[str], dict[str, str] | str | None],
                             *, refresh: Callable[..., Any] | None = None,
                             on_refresh: Callable[[str], None] | None = None) -> list[ResolvedItem]:
        """Resolve an account reference without making its value durable."""
        raw = credential_resolver(context.account_ref) if context.account_ref else None
        credentials = raw if isinstance(raw, dict) else ({"credential": raw} if raw else {})
        try:
            return self.resolve(context, resolver, credentials=credentials, refresh=refresh,
                                on_refresh=on_refresh)
        finally:
            credentials.clear()


class SessionBroker:
    """Tracks opaque browser/account sessions without storing their tokens."""
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    def register(self, reference: str, provider_id: str, expires_at: float | None = None,
                 account_ref: str | None = None) -> dict[str, Any]:
        self._sessions[reference] = {"reference": reference, "provider_id": provider_id,
                                     "expires_at": expires_at, "account_ref": account_ref, "state": "active"}
        return dict(self._sessions[reference])

    def status(self, reference: str, now: float | None = None) -> dict[str, Any] | None:
        value = self._sessions.get(reference)
        if not value:
            return None
        if value.get("expires_at") is not None and float(value["expires_at"]) <= (now or time.time()):
            value["state"] = "expired"
        return dict(value)

    def refresh(self, reference: str, *, expires_at: float | None = None,
                state: str = "active") -> dict[str, Any]:
        value = self._sessions.get(reference)
        if not value:
            raise KeyError("browser session not found")
        value["expires_at"], value["state"] = expires_at, state
        return dict(value)
