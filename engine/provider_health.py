"""Provider health aggregation and canary state."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from .models import ProviderHealthSnapshot


class ProviderHealthMonitor:
    def __init__(self, store: Any | None = None) -> None:
        self.store = store
        self._data: dict[str, ProviderHealthSnapshot] = {}

    def record(self, provider_id: str, outcome: str, category: str | None = None,
               version: str | None = None, error: str | None = None) -> ProviderHealthSnapshot:
        snapshot = self._data.setdefault(provider_id, ProviderHealthSnapshot(provider_id))
        snapshot.total_attempts += 1
        if outcome in {"success", "completed", "ok"}:
            snapshot.successes += 1
        else:
            key = category or outcome or "unknown"
            snapshot.failure_categories[key] = snapshot.failure_categories.get(key, 0) + 1
        snapshot.success_rate = snapshot.successes / snapshot.total_attempts if snapshot.total_attempts else 0.0
        snapshot.last_known_version = version or snapshot.last_known_version
        snapshot.last_error = error
        snapshot.checked_at = time.time()
        if self.store and hasattr(self.store, "save_provider_health_snapshot"):
            self.store.save_provider_health_snapshot(snapshot)
        return snapshot

    def set_quarantine(self, provider_id: str, quarantined: bool) -> ProviderHealthSnapshot:
        snapshot = self._data.setdefault(provider_id, ProviderHealthSnapshot(provider_id))
        snapshot.quarantined = quarantined
        snapshot.checked_at = time.time()
        if self.store and hasattr(self.store, "save_provider_health_snapshot"):
            self.store.save_provider_health_snapshot(snapshot)
        return snapshot

    def record_plugin_fault(self, provider_id: str, error: str, *, threshold: int = 3,
                            version: str | None = None) -> ProviderHealthSnapshot:
        """Classify implementation faults separately from provider outcomes."""
        snapshot = self.record(provider_id, "plugin_fault", category="plugin_fault",
                               version=version, error=error[:500])
        faults = snapshot.failure_categories.get("plugin_fault", 0)
        if faults >= threshold:
            snapshot.quarantined = True
            if self.store and hasattr(self.store, "save_provider_health_snapshot"):
                self.store.save_provider_health_snapshot(snapshot)
        return snapshot

    def is_quarantined(self, provider_id: str) -> bool:
        snapshot = self._data.get(provider_id)
        if snapshot is not None:
            return snapshot.quarantined
        for value in self.snapshots():
            if value.get("provider_id") == provider_id:
                return bool(value.get("quarantined", False))
        return False

    def snapshots(self) -> list[dict[str, Any]]:
        persisted = self.store.list_provider_health_snapshots() if self.store and hasattr(self.store, "list_provider_health_snapshots") else []
        values = {row["provider_id"]: row for row in persisted}
        values.update({key: value.to_dict() for key, value in self._data.items()})
        return [values[key] for key in sorted(values)]
