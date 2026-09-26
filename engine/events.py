from __future__ import annotations

from typing import Any

from .db import TaskStore


class EventPublisher:
    """Durable event outbox facade; UI polling can acknowledge after rendering."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def emit(self, event_type: str, task_id: str | None, payload: dict[str, Any], dedupe_key: str | None = None) -> int:
        return self.store.enqueue_event(event_type, task_id, payload, dedupe_key)

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.pending_events(limit)

    def acknowledge(self, event_id: int) -> None:
        self.store.mark_event_published(event_id)

    def events_since(self, event_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.events_since(event_id, limit)

