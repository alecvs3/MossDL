"""Policy for retrying a failed transfer on a second transport.

The Rust core handles nearly all traffic. The Python transport stays as a
break-glass path, and the only version of that which keeps working is one the
engine exercises itself: a fallback nobody ever runs is a fallback that has
quietly rotted by the time it is needed.

So when the preferred backend fails for a transport reason, the engine retries
once on the fallback and records both halves. That keeps the second path warm
against real servers and produces the evidence for whether it earns its keep.

Deciding *not* to fall back matters as much as deciding to. Pausing, cancelling
and expired-URL failures are all handled by layers above this one; retrying
them here would paper over a pause or burn an attempt on a link that simply
needs re-resolving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import DownloadCanceled, DownloadPaused

# Categories the transport layer classifies for itself. ``throttle`` and
# ``auth`` describe the *server's* answer, not a defect in the client, so a
# second client would be told exactly the same thing.
_PROVIDER_OWNED_CATEGORIES = {"throttle", "auth"}

# Substrings marking a URL that has aged out. The refresh loop re-resolves
# these; a different transport would only fetch the same dead link.
_EXPIRED_MARKERS = ("expired", "link is no longer", "url has expired")

# Substrings marking content integrity failures. A second transport client
# will receive the exact same bytes from the server and fail the same hash.
_INTEGRITY_MARKERS = ("checksum mismatch", "hash mismatch")


@dataclass(frozen=True, slots=True)
class FallbackDecision:
    """Why a failure will or will not be retried on another backend."""

    should_fallback: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"should_fallback": self.should_fallback, "reason": self.reason}


def classify_failure(error: BaseException) -> FallbackDecision:
    """Decide whether ``error`` is worth a second attempt on another transport."""
    if isinstance(error, (DownloadCanceled, DownloadPaused)):
        return FallbackDecision(False, "transfer was paused or canceled by the user")

    category = str(getattr(error, "category", "") or "").lower()
    if category in _PROVIDER_OWNED_CATEGORIES:
        return FallbackDecision(False, f"{category} is the server's answer, not a transport defect")

    status = getattr(error, "status_code", None)
    text = str(error).lower()
    if status in {401, 403, 410} or any(marker in text for marker in _EXPIRED_MARKERS):
        return FallbackDecision(False, "expired or unauthorized source; refreshing the URL is the fix")

    if any(marker in text for marker in _INTEGRITY_MARKERS):
        return FallbackDecision(False, "content integrity failed; second transport cannot change hash")

    return FallbackDecision(True, _describe(error))


def _describe(error: BaseException) -> str:
    """A short, quotable reason for the telemetry record."""
    detail = " ".join(str(error).split())[:200]
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


def fallback_backend(primary: str, capabilities_for_item: dict[str, bool]) -> str | None:
    """Name the backend to retry on, or None when no other one can take it.

    ``capabilities_for_item`` maps a backend name to whether it satisfies this
    particular item, so the caller stays responsible for capability checks and
    this module stays responsible only for ordering.
    """
    for candidate in ("custom", "rust"):
        if candidate != primary and capabilities_for_item.get(candidate):
            return candidate
    return None


__all__ = ["FallbackDecision", "classify_failure", "fallback_backend"]
