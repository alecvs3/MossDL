from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RangeCapability(str, Enum):
    """The safety decision made from an HTTP range probe."""

    VALID_RANGED = "valid-ranged"
    SEQUENTIAL_ONLY = "sequential-only"
    AMBIGUOUS = "ambiguous"
    VALIDATOR_CONFLICT = "validator-conflict"
    INVALID_416 = "invalid-416"


_CONTENT_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$", re.IGNORECASE)
_UNSATISFIABLE_RANGE = re.compile(r"^bytes\s+\*/(\d+)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RangeProbeResult:
    capability: RangeCapability
    size: int | None
    status_code: int | None
    validator: str | None = None
    content_range: str | None = None
    reason: str = ""

    @property
    def supports_ranges(self) -> bool:
        return self.capability is RangeCapability.VALID_RANGED

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability.value,
            "size": self.size,
            "status_code": self.status_code,
            "validator": self.validator,
            "content_range": self.content_range,
            "reason": self.reason,
        }


@dataclass(slots=True)
class RangePlan:
    """A bounded prefix of work plus the policy used to extend it."""

    total_size: int
    ranges: list[tuple[int, int]]
    capability: RangeCapability = RangeCapability.VALID_RANGED
    validator: str | None = None
    initial_concurrency: int = 1
    max_concurrency: int = 1
    min_range_size: int = 1
    max_range_size: int = 1
    target_duration_seconds: float = 4.0
    observed_bytes_per_second: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def initial_ranges(self) -> list[tuple[int, int]]:
        return list(self.ranges)

    @property
    def is_ranged(self) -> bool:
        return self.capability is RangeCapability.VALID_RANGED

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_size": self.total_size,
            "ranges": [{"start": start, "end": end} for start, end in self.ranges],
            "capability": self.capability.value,
            "validator": self.validator,
            "initial_concurrency": self.initial_concurrency,
            "max_concurrency": self.max_concurrency,
            "min_range_size": self.min_range_size,
            "max_range_size": self.max_range_size,
            "target_duration_seconds": self.target_duration_seconds,
            "observed_bytes_per_second": self.observed_bytes_per_second,
            "diagnostics": dict(self.diagnostics),
        }


class RangePlanner:
    """Validate range semantics and produce bounded, telemetry-sized work."""

    def __init__(
        self,
        *,
        min_range_size: int = 4 * 1024 * 1024,
        max_range_size: int = 64 * 1024 * 1024,
        target_duration_seconds: float = 4.0,
        initial_concurrency: int = 2,
        max_concurrency: int = 8,
    ) -> None:
        if min_range_size <= 0 or max_range_size < min_range_size:
            raise ValueError("range bounds must be positive and ordered")
        if target_duration_seconds <= 0:
            raise ValueError("target_duration_seconds must be positive")
        self.min_range_size = int(min_range_size)
        self.max_range_size = int(max_range_size)
        self.target_duration_seconds = float(target_duration_seconds)
        self.initial_concurrency = max(1, int(initial_concurrency))
        self.max_concurrency = max(self.initial_concurrency, int(max_concurrency))
        self._lock = threading.Lock()
        self._observed_bytes = 0
        self._observed_seconds = 0.0

    @staticmethod
    def _headers(result: Mapping[str, Any]) -> dict[str, str]:
        raw = result.get("headers") or {}
        return {str(key).lower(): str(value).strip() for key, value in raw.items()}

    @staticmethod
    def validate_content_range(value: str, start: int, end: int, total_size: int | None) -> bool:
        match = _CONTENT_RANGE.fullmatch(str(value).strip())
        if not match or int(match.group(1)) != int(start) or int(match.group(2)) != int(end):
            return False
        return total_size is None or int(match.group(3)) == int(total_size)

    @staticmethod
    def _validator(result: Mapping[str, Any], headers: Mapping[str, str]) -> str | None:
        return (str(result.get("validator")).strip() if result.get("validator") else None) or \
            (headers.get("etag") or headers.get("last-modified") or None)

    @staticmethod
    def _size(result: Mapping[str, Any], headers: Mapping[str, str], content_range: str) -> int | None:
        value = result.get("size")
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        match = _CONTENT_RANGE.fullmatch(content_range)
        if match:
            return int(match.group(3))
        match = _UNSATISFIABLE_RANGE.fullmatch(content_range)
        if match:
            return int(match.group(1))
        length = headers.get("content-length")
        return int(length) if length and length.isdigit() else None

    def probe_and_classify(
        self,
        result: Mapping[str, Any],
        *,
        expected_size: int | None = None,
        expected_validator: str | None = None,
    ) -> RangeProbeResult:
        """Turn an untrusted HEAD/range response into an explicit decision."""
        headers = self._headers(result)
        status_value = result.get("status_code", result.get("status"))
        try:
            status = int(status_value) if status_value is not None else None
        except (TypeError, ValueError):
            status = None
        content_range = str(result.get("content_range") or headers.get("content-range") or "").strip()
        size = self._size(result, headers, content_range)
        validator = self._validator(result, headers)
        if expected_validator and validator and validator != expected_validator:
            return RangeProbeResult(RangeCapability.VALIDATOR_CONFLICT, size, status, validator,
                                    content_range, "probe validator differs from the durable validator")
        if expected_validator and not validator:
            return RangeProbeResult(RangeCapability.VALIDATOR_CONFLICT, size, status, None,
                                    content_range, "probe omitted the durable validator")
        if expected_size is not None and size is not None and size != expected_size:
            return RangeProbeResult(RangeCapability.AMBIGUOUS, size, status, validator,
                                    content_range, "probe size differs from the resolved item")

        if status == 416:
            return RangeProbeResult(RangeCapability.INVALID_416, size, status, validator,
                                    content_range, "range probe was rejected with 416")
        if status == 206:
            match = _CONTENT_RANGE.fullmatch(content_range)
            if not match or int(match.group(1)) != 0 or int(match.group(2)) != 0:
                return RangeProbeResult(RangeCapability.AMBIGUOUS, size, status, validator,
                                        content_range, "206 probe did not return exactly bytes 0-0")
            if int(match.group(3)) <= 0:
                return RangeProbeResult(RangeCapability.AMBIGUOUS, size, status, validator,
                                        content_range, "206 probe total size is invalid")
            length = headers.get("content-length")
            if length and length.isdigit() and int(length) != 1:
                return RangeProbeResult(RangeCapability.AMBIGUOUS, size, status, validator,
                                        content_range, "206 probe returned a body other than one byte")
            return RangeProbeResult(RangeCapability.VALID_RANGED, int(match.group(3)), status, validator,
                                    content_range, "validated 206 bytes 0-0 probe")
        if status == 200 and not bool(result.get("ranges")):
            return RangeProbeResult(RangeCapability.SEQUENTIAL_ONLY, size, status, validator,
                                    content_range, "server does not advertise usable byte ranges")
        if status in {405, 501} or result.get("ranges") is False:
            return RangeProbeResult(RangeCapability.SEQUENTIAL_ONLY, size, status, validator,
                                    content_range, "server does not support range probing")
        return RangeProbeResult(RangeCapability.AMBIGUOUS, size, status, validator,
                                content_range, "range capability could not be validated")

    def _range_size(self, observed_bytes_per_second: float = 0.0) -> int:
        with self._lock:
            if observed_bytes_per_second <= 0 and self._observed_seconds > 0:
                observed_bytes_per_second = self._observed_bytes / self._observed_seconds
        target = max(self.min_range_size, int(observed_bytes_per_second * self.target_duration_seconds))
        return min(self.max_range_size, target)

    def observe(self, transferred_bytes: int, elapsed_seconds: float) -> None:
        if transferred_bytes < 0 or elapsed_seconds <= 0:
            return
        with self._lock:
            self._observed_bytes += int(transferred_bytes)
            self._observed_seconds += float(elapsed_seconds)

    def observed_rate(self) -> float:
        with self._lock:
            return self._observed_bytes / self._observed_seconds if self._observed_seconds > 0 else 0.0

    def plan_initial_ranges(
        self,
        total_size: int,
        *,
        capability: RangeCapability = RangeCapability.VALID_RANGED,
        validator: str | None = None,
        observed_bytes_per_second: float = 0.0,
    ) -> RangePlan:
        if total_size < 0:
            raise ValueError("total_size must be non-negative")
        max_concurrency = max(1, self.max_concurrency)
        initial_concurrency = min(self.initial_concurrency, max_concurrency, max(1, total_size)) if total_size else 1
        if capability is not RangeCapability.VALID_RANGED or total_size == 0:
            return RangePlan(total_size, [], capability, validator, 1, max_concurrency,
                             self.min_range_size, self.max_range_size, self.target_duration_seconds,
                             observed_bytes_per_second, {"downgraded": capability.value != RangeCapability.VALID_RANGED})
        chunk_size = self._range_size(observed_bytes_per_second)
        ramp_bytes = min(total_size, chunk_size * initial_concurrency)
        ranges = self._partition(ramp_bytes, chunk_size)
        return RangePlan(total_size, ranges, capability, validator, initial_concurrency, max_concurrency,
                         self.min_range_size, self.max_range_size, self.target_duration_seconds,
                         observed_bytes_per_second, {"ramp_bytes": ramp_bytes, "chunk_size": chunk_size})

    def next_range(
        self,
        cursor: int,
        total_size: int,
        *,
        observed_bytes_per_second: float = 0.0,
        capability: RangeCapability = RangeCapability.VALID_RANGED,
    ) -> tuple[int, int] | None:
        if capability is not RangeCapability.VALID_RANGED:
            return None
        if cursor < 0 or total_size < 0 or cursor > total_size:
            raise ValueError("range cursor is outside the file")
        if cursor == total_size:
            return None
        end = min(total_size - 1, cursor + self._range_size(observed_bytes_per_second) - 1)
        return cursor, end

    def resize_future_range(
        self,
        cursor: int,
        total_size: int,
        *,
        observed_bytes_per_second: float = 0.0,
    ) -> tuple[int, int] | None:
        """Alias emphasizing that only unplanned future work may be resized."""
        return self.next_range(cursor, total_size, observed_bytes_per_second=observed_bytes_per_second)

    def _partition(self, total_size: int, chunk_size: int) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        cursor = 0
        while cursor < total_size:
            end = min(total_size - 1, cursor + chunk_size - 1)
            result.append((cursor, end))
            cursor = end + 1
        return result


__all__ = ["RangeCapability", "RangePlan", "RangePlanner"]
