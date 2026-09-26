from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from .resume_identity import file_identity, sync_data

logger = logging.getLogger(__name__)


@dataclass
class SegmentState:
    id: int
    start: int
    end: int
    done: int = 0
    worker_id: str | None = None
    status: str = "pending"  # "pending", "active", "completed"

    @property
    def expected(self) -> int:
        return self.end - self.start + 1

    @property
    def remaining(self) -> int:
        return max(0, self.expected - self.done)


class DynamicSegmentCoordinator:
    """
    Dynamic Work-Stealing Segment Coordinator.
    Coordinates multi-connection segment downloads with dynamic tail-end splitting.
    When a worker finishes early, it steals the uncompleted tail of the slowest
    in-flight segment, ensuring 100% thread saturation until the last byte is downloaded.
    """

    BLOCK_ALIGN = 64 * 1024  # 64 KB block alignment for steal boundaries

    def __init__(
        self,
        manifest_path: Path,
        total_size: int,
        min_steal_bytes: int = 8 * 1024 * 1024,  # 8 MB minimum chunk to justify splitting
        flush_interval: float = 2.0,  # seconds between manifest flushes
        data_path: Path | None = None,
        identity: str | None = None,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.total_size = total_size
        self.min_steal_bytes = min_steal_bytes
        if self.total_size < 0:
            raise ValueError("total_size must be non-negative")
        if self.min_steal_bytes <= 0:
            raise ValueError("min_steal_bytes must be positive")
        self.segments: list[SegmentState] = []
        self._lock = threading.Lock()
        self._next_id = 0
        self._dirty = False
        self._last_flush = 0.0
        self._flush_interval = flush_interval
        self.data_path, self.identity = data_path, identity

    def load_or_init(self, initial_ranges: list[tuple[int, int]]) -> None:
        """Loads an existing segment manifest if present and matching, or initializes new ranges."""
        with self._lock:
            if self.manifest_path.exists():
                try:
                    data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                    if self.data_path is not None and (
                        data.get('schema') != 2 or data.get('identity') != self.identity
                        or file_identity(self.data_path) is None
                        or data.get('file_identity') != file_identity(self.data_path)
                        or self.data_path.stat().st_size != self.total_size
                    ):
                        raise ValueError('checkpoint content or partial-file identity changed')
                    if data.get("total_size") == self.total_size and "segments" in data:
                        loaded: list[SegmentState] = []
                        for value in data["segments"]:
                            segment = SegmentState(
                                id=int(value["id"]),
                                start=int(value["start"]),
                                end=int(value["end"]),
                                done=int(value.get("done", 0)),
                                status="completed" if int(value.get("done", 0)) >= (int(value["end"]) - int(value["start"]) + 1) else "pending",
                            )
                            if segment.start < 0 or segment.end < segment.start or segment.end >= self.total_size:
                                raise ValueError("segment range is outside the file")
                            if segment.done < 0 or segment.done > segment.expected:
                                raise ValueError("segment progress is outside its range")
                            loaded.append(segment)
                        self._validate_layout(loaded)
                        self.segments = loaded
                        self._next_id = max((s.id for s in self.segments), default=0) + 1
                        logger.debug("Resumed existing segment manifest with %d segments", len(self.segments))
                        return
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    logger.warning("[RESUME_INVALIDATED] %s: %s", self.manifest_path.name, exc)

            self.segments = []
            for i, (start, end) in enumerate(initial_ranges):
                self.segments.append(SegmentState(id=i, start=start, end=end))
            self._validate_layout(self.segments)
            self._next_id = len(self.segments)
            self._save_unlocked()

    def claim_work(self, worker_id: str) -> SegmentState | None:
        """
        Assigns the next available segment to the worker.
        If no pending segments exist, attempts work-stealing from in-flight segments.
        """
        with self._lock:
            # 1. Look for pending segments
            for seg in self.segments:
                if seg.status == "pending":
                    seg.status = "active"
                    seg.worker_id = worker_id
                    self._save_unlocked()
                    return seg

            # 2. Try work-stealing from the largest in-flight active segment
            return self._try_steal_unlocked(worker_id)

    def next_unplanned_offset(self) -> int:
        """Return the first byte not represented by the durable manifest."""
        with self._lock:
            return max((segment.end for segment in self.segments), default=-1) + 1

    def append_range(self, start: int, end: int) -> bool:
        """Append one contiguous future range without rewriting existing work."""
        with self._lock:
            expected_start = max((segment.end for segment in self.segments), default=-1) + 1
            if start != expected_start:
                return False
            if start < 0 or end < start or end >= self.total_size:
                raise ValueError("future segment range is outside the file")
            self.segments.append(SegmentState(id=self._next_id, start=start, end=end))
            self._next_id += 1
            self._validate_layout(self.segments)
            self._save_unlocked()
            return True

    def _try_steal_unlocked(self, worker_id: str) -> SegmentState | None:
        """Splits the largest active segment with sufficient remaining work.

        The split point is aligned UP to the next 64 KB boundary so that both
        the victim and thief operate on filesystem-block-aligned ranges.
        """
        best_candidate: SegmentState | None = None
        max_remaining = 0

        for seg in self.segments:
            if seg.status == "active":
                rem = seg.remaining
                if rem > max_remaining:
                    max_remaining = rem
                    best_candidate = seg

        if best_candidate and max_remaining >= (2 * self.min_steal_bytes):
            # Split point in the middle of remaining bytes
            steal_size = max_remaining // 2
            raw_split = best_candidate.end - steal_size + 1

            # Align UP to next 64 KB boundary
            split_start = (raw_split + self.BLOCK_ALIGN - 1) & ~(self.BLOCK_ALIGN - 1)

            # If alignment pushes past end, this steal isn't viable
            if split_start > best_candidate.end:
                return None

            # Ensure the victim retains at least min_steal_bytes after the split
            victim_remaining_after = split_start - (best_candidate.start + best_candidate.done)
            if victim_remaining_after < 0:
                return None

            # Truncate existing segment's target end
            old_end = best_candidate.end
            best_candidate.end = split_start - 1

            # Create new stolen segment for worker
            new_seg = SegmentState(
                id=self._next_id,
                start=split_start,
                end=old_end,
                done=0,
                worker_id=worker_id,
                status="active",
            )
            self._next_id += 1
            self.segments.append(new_seg)
            self._save_unlocked()

            logger.info(
                "Work-stealing: worker %s stole [%d..%d] (%d bytes) from segment %d (aligned to 64KB)",
                worker_id, new_seg.start, new_seg.end, new_seg.expected, best_candidate.id
            )
            return new_seg

        return None

    def record_progress(self, segment_id: int, written_delta: int) -> None:
        if written_delta < 0:
            raise ValueError("written_delta must be non-negative")
        with self._lock:
            for seg in self.segments:
                if seg.id == segment_id:
                    seg.done = min(seg.expected, seg.done + written_delta)
                    if seg.done >= seg.expected:
                        seg.status = "completed"
                    break
            self._dirty = True
            now = time.monotonic()
            if now - self._last_flush >= self._flush_interval:
                self._save_unlocked()
                self._dirty = False
                self._last_flush = now

    def flush(self) -> None:
        """Force-save dirty state to disk."""
        with self._lock:
            if self._dirty:
                self._save_unlocked()
                self._dirty = False
                self._last_flush = time.monotonic()

    def complete_segment(self, segment_id: int) -> None:
        with self._lock:
            for seg in self.segments:
                if seg.id == segment_id:
                    seg.status = "completed"
                    seg.done = seg.expected
                    break
            self._save_unlocked()

    def release_segment(self, segment_id: int) -> None:
        """Releases an active segment back to pending if a worker failed or interrupted."""
        with self._lock:
            for seg in self.segments:
                if seg.id == segment_id and seg.status != "completed":
                    seg.status = "pending"
                    seg.worker_id = None
                    break
            self._save_unlocked()

    def is_all_done(self) -> bool:
        with self._lock:
            return all(seg.status == "completed" for seg in self.segments) and (
                sum(seg.done for seg in self.segments) == self.total_size
            )

    def total_bytes_done(self) -> int:
        with self._lock:
            return sum(seg.done for seg in self.segments)

    def _validate_layout(self, segments: list[SegmentState]) -> None:
        if not segments and self.total_size != 0:
            raise ValueError("segment manifest is empty for a non-empty file")
        ordered = sorted(segments, key=lambda segment: (segment.start, segment.end, segment.id))
        if len({segment.id for segment in ordered}) != len(ordered):
            raise ValueError("segment IDs must be unique")
        cursor = 0
        for segment in ordered:
            if segment.start != cursor:
                raise ValueError("segment manifest contains a gap or overlap")
            cursor = segment.end + 1
        # A caller may seed only the ranges known so far (for example while a
        # metadata probe is still completing). The coordinator must never
        # accept an overlap or a range beyond the declared file size, but a
        # contiguous prefix can be resumed and extended safely.
        if cursor > self.total_size:
            raise ValueError("segment manifest exceeds the declared file size")

    def _save_unlocked(self) -> None:
        tmp = self.manifest_path.with_name(self.manifest_path.name + ".tmp")
        payload = {
            "schema": 2,
            "identity": self.identity,
            "file_identity": file_identity(self.data_path) if self.data_path else None,
            "total_size": self.total_size,
            "segments": [asdict(s) for s in self.segments],
        }
        try:
            if self.data_path is not None and any(s.done for s in self.segments):
                sync_data(self.data_path)
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, self.manifest_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        except OSError as exc:
            raise RuntimeError(f"could not persist segment manifest: {self.manifest_path}") from exc
