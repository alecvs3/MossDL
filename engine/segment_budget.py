"""Choose per-file transport fan-out without hiding it in a backend.

The scheduler admits resolved *items*, while a segmented backend may open many
HTTP connections for each admitted item.  Provider-specific allocation belongs
here so item concurrency and transport concurrency describe the same resource.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SegmentBudget:
    max_segments: int
    reason: str


def segment_budget(provider: str, *, is_multipart: bool) -> SegmentBudget | None:
    """Return an explicit backend segment cap when provider evidence requires it."""
    normalized = str(provider or "").strip().lower()
    if normalized == "datanodes" and is_multipart:
        # DataNodes assigns a lane to each independently resolved file/session.
        # Multiple ranges inside one file consume transport capacity that should
        # instead admit sibling files.  The reference client proven to run four
        # parts concurrently uses one HTTP connection per file.
        return SegmentBudget(1, "multipart_file_parallelism")
    return None
