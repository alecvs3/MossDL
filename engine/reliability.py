"""Provider-neutral reliability policies and file-integrity primitives."""

from __future__ import annotations

import hashlib
import mimetypes
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable


class FailureClass(StrEnum):
    TRANSIENT_NETWORK = "transient_network"
    RATE_LIMITED = "rate_limited"
    EXPIRED_URL = "expired_url"
    EXPIRED_SESSION = "expired_session"
    AUTHENTICATION = "authentication"
    QUOTA = "quota"
    PROVIDER_REJECTED = "provider_rejected"
    INVALID_CONTENT = "invalid_content"
    DESTINATION = "destination"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    jitter_ratio: float = 0.2
    refresh_expired_urls: bool = True
    switch_accounts: bool = True
    switch_candidates: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": max(1, int(self.max_attempts)),
            "base_delay_seconds": max(0.0, float(self.base_delay_seconds)),
            "max_delay_seconds": max(0.0, float(self.max_delay_seconds)),
            "jitter_ratio": max(0.0, min(1.0, float(self.jitter_ratio))),
            "refresh_expired_urls": bool(self.refresh_expired_urls),
            "switch_accounts": bool(self.switch_accounts),
            "switch_candidates": bool(self.switch_candidates),
        }


@dataclass(slots=True)
class RetryDecision:
    failure: FailureClass
    retry: bool
    refresh_session: bool = False
    switch_account: bool = False
    switch_candidate: bool = False
    delay_seconds: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"failure": self.failure.value, "retry": self.retry,
                "refresh_session": self.refresh_session,
                "switch_account": self.switch_account,
                "switch_candidate": self.switch_candidate,
                "delay_seconds": self.delay_seconds, "reason": self.reason}


_THROTTLE_MARKERS = (
    "too many requests", "rate limit", "ratelimit", "rate-limit", "slow down",
    "try again later", "retry after", "retry-after", "just a moment", "captcha",
    "challenge", "cloudflare", "attention required", "throttl", "bandwidth limit",
    "download limit", "traffic limit", "quota", "too many users", "temporarily blocked",
)
_RETRYABLE_TRANSPORT_MARKERS = (
    "timed out", "timeout", "connection reset", "connection refused", "connection aborted",
    "temporarily", "network", "unexpected eof", "server error", "bad gateway",
    "service unavailable", "gateway timeout",
)
_AUTH_STATUSES = {401, 407}


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header value as delta-seconds or an HTTP-date."""
    if value is None:
        return None
    token = str(value).strip()
    if not token:
        return None
    try:
        return max(0.0, float(token))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(token)
    except (TypeError, ValueError, IndexError):
        return None
    if target is None:
        return None
    now = datetime.now(timezone.utc) if target.tzinfo is not None else datetime.utcnow()
    return max(0.0, (target - now).total_seconds())


@dataclass(frozen=True, slots=True)
class TransportSignal:
    """One classified transport response shared by every backend.

    ``category`` is one of ``throttle``, ``retryable``, ``auth`` or ``terminal``.
    Throttle signals are retryable by contract so a 429/403 block page can never
    be mistaken for a terminal provider rejection.
    """

    category: str
    retry_after_seconds: float | None = None
    status_code: int | None = None
    evidence: str = ""

    @property
    def retryable(self) -> bool:
        return self.category in {"throttle", "retryable"}

    @property
    def failure_class(self) -> FailureClass:
        return {
            "throttle": FailureClass.RATE_LIMITED,
            "retryable": FailureClass.TRANSIENT_NETWORK,
            "auth": FailureClass.AUTHENTICATION,
            "terminal": FailureClass.PROVIDER_REJECTED,
        }.get(self.category, FailureClass.UNKNOWN)

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category, "retry_after_seconds": self.retry_after_seconds,
                "status_code": self.status_code, "evidence": self.evidence}


class TransportSignalError(RuntimeError):
    """A classified transport failure carrying its structured signal."""

    def __init__(self, signal: TransportSignal, message: str | None = None) -> None:
        super().__init__(message or signal.evidence or f"transport signal {signal.category}")
        self.transport_signal = signal
        self.category = signal.category
        self.status_code = signal.status_code
        self.retry_after = signal.retry_after_seconds


def classify_transport_signal(status: int | str | None, text: str = "",
                              headers: dict[str, Any] | None = None) -> TransportSignal:
    """Classify one HTTP response / transport error into a stable category."""
    status_int: int | None
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    headers_map = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    lower = str(text or "").lower()
    content_type = headers_map.get("content-type", "").lower()
    retry_after = parse_retry_after(headers_map.get("retry-after"))
    if retry_after is None:
        retry_after = parse_retry_after(headers_map.get("x-retry-after"))
    marker = next((candidate for candidate in _THROTTLE_MARKERS if candidate in lower), None)
    retry_marker = next((candidate for candidate in _RETRYABLE_TRANSPORT_MARKERS if candidate in lower), None)
    html_like = content_type.startswith("text/html") or lower.lstrip().startswith(
        ("<html", "<!doctype html", "<head", "<body"))

    if status_int in {429, 509}:
        category, reason = "throttle", f"HTTP {status_int} rate limited"
    elif status_int == 403 and (marker or html_like):
        category, reason = "throttle", "HTTP 403 provider block page"
    elif status_int == 403 and retry_after is not None:
        # An explicit Retry-After is the server asking us to come back, not a
        # refusal -- the same reading already applied to 5xx below. Without this,
        # a single throttled range request is classified terminal and kills a
        # multi-GB transfer that would have succeeded after the backoff.
        category, reason = "throttle", "HTTP 403 with Retry-After"
    elif status_int in _AUTH_STATUSES:
        category, reason = "auth", f"HTTP {status_int} authentication required"
    elif status_int is not None and status_int >= 500:
        if retry_after is not None:
            category, reason = "throttle", f"HTTP {status_int} with Retry-After"
        else:
            category, reason = "retryable", f"HTTP {status_int} transient server error"
    elif status_int in {408, 425}:
        category, reason = "retryable", f"HTTP {status_int} transient request error"
    elif status_int is not None and 400 <= status_int < 500:
        category, reason = "terminal", f"HTTP {status_int} client error"
    elif retry_marker:
        category, reason = "retryable", f"transport error ({retry_marker})"
    else:
        category, reason = "terminal", "unclassified transport signal"

    evidence_parts = [reason]
    if retry_after is not None:
        evidence_parts.append(f"Retry-After={retry_after:.3f}s")
    if marker and marker not in reason:
        evidence_parts.append(f"marker={marker!r}")
    if html_like:
        evidence_parts.append("body=html")
    return TransportSignal(category, retry_after, status_int, "; ".join(evidence_parts)[:300])


def decorrelated_jitter(previous_delay: float = 0.0, *, base_seconds: float = 1.0,
                        max_seconds: float = 60.0,
                        rng: Callable[[float, float], float] | None = None) -> float:
    """AWS-style decorrelated jitter: upper bound grows from the previous delay."""
    base = max(0.001, float(base_seconds))
    cap = max(base, float(max_seconds))
    previous = max(0.0, float(previous_delay or 0.0))
    upper = min(cap, max(base, previous * 3.0))
    sampler = rng or random.uniform
    return min(cap, max(base, float(sampler(base, upper))))


def classify_failure(error: BaseException | str, status_code: int | None = None) -> FailureClass:
    signal = getattr(error, "transport_signal", None)
    if isinstance(signal, TransportSignal) and signal.category != "terminal":
        return signal.failure_class
    failure = _classify_failure_text(error, status_code)
    if isinstance(signal, TransportSignal) and failure == FailureClass.UNKNOWN:
        return signal.failure_class
    return failure


def _classify_failure_text(error: BaseException | str, status_code: int | None = None) -> FailureClass:
    text = str(error).lower()
    status = status_code or getattr(error, "status_code", None) or getattr(error, "code", None)
    category = getattr(error, "category", None)
    if category in {"provider_rejected", "not_found"} or status == 404 or any(x in text for x in ("provider rejected", "not found", "file not found", "file was deleted", "file expired", "no such file", "removed", "dmca", "taken down")):
        return FailureClass.PROVIDER_REJECTED
    if category == "transient_network":
        return FailureClass.TRANSIENT_NETWORK
    if category == "authentication":
        return FailureClass.AUTHENTICATION
    if category in {"quota", "rate_limited"} or status in {429, 509} or any(x in text for x in ("quota", "bandwidth limit", "traffic exhausted", "too many users")):
        return FailureClass.QUOTA if category == "quota" or "quota" in text or "too many users" in text else FailureClass.RATE_LIMITED
    if status in {401} or any(x in text for x in ("unauthorized", "authentication", "login required", "invalid token")):
        return FailureClass.AUTHENTICATION
    if (status in {403, 410} or any(x in text for x in ("url expired", "link expired", "signature", "signed url"))) and any(x in text for x in ("signature", "signed", "url", "link")):
        return FailureClass.EXPIRED_URL
    if any(x in text for x in ("session expired", "browser session", "cookie expired")):
        return FailureClass.EXPIRED_SESSION
    if any(x in text for x in ("unsupported", "drm", "encrypted")):
        return FailureClass.UNSUPPORTED
    if any(x in text for x in ("content-type", "html response", "checksum", "corrupt", "truncated")):
        return FailureClass.INVALID_CONTENT
    if any(x in text for x in ("no space", "destination", "permission denied", "filename")):
        return FailureClass.DESTINATION
    if status is not None and int(status) >= 500:
        return FailureClass.TRANSIENT_NETWORK
    if any(x in text for x in ("timed out", "timeout", "connection reset", "temporarily", "network")):
        return FailureClass.TRANSIENT_NETWORK
    if any(x in text for x in ("provider rejected", "private", "forbidden")):
        return FailureClass.PROVIDER_REJECTED
    return FailureClass.UNKNOWN


def decide_retry(error: BaseException | str, attempt: int, policy: RetryPolicy | None = None,
                 status_code: int | None = None, retry_after: float | None = None) -> RetryDecision:
    policy = policy or RetryPolicy()
    failure = classify_failure(error, status_code)
    retryable = {FailureClass.TRANSIENT_NETWORK, FailureClass.RATE_LIMITED,
                 FailureClass.EXPIRED_URL, FailureClass.EXPIRED_SESSION,
                 FailureClass.INVALID_CONTENT}
    if failure in {FailureClass.AUTHENTICATION, FailureClass.QUOTA}:
        retryable.add(failure)
    if attempt >= max(1, policy.max_attempts) or failure not in retryable:
        return RetryDecision(failure, False, reason="failure is terminal or retry budget is exhausted")
    delay = float(retry_after) if retry_after is not None else min(
        policy.max_delay_seconds, policy.base_delay_seconds * (2 ** max(0, attempt)))
    if retry_after is None and delay > 0 and policy.jitter_ratio:
        delay *= 1.0 + random.uniform(-policy.jitter_ratio, policy.jitter_ratio)
    delay = min(policy.max_delay_seconds, max(0.0, delay))
    return RetryDecision(
        failure, True,
        refresh_session=failure in {FailureClass.EXPIRED_URL, FailureClass.EXPIRED_SESSION} and policy.refresh_expired_urls,
        switch_account=failure in {FailureClass.AUTHENTICATION, FailureClass.QUOTA} and policy.switch_accounts,
        switch_candidate=failure in {FailureClass.EXPIRED_URL, FailureClass.PROVIDER_REJECTED} and policy.switch_candidates,
        delay_seconds=max(0.0, delay), reason="retryable provider/network failure")


@dataclass(slots=True)
class DownloadIntegrityReport:
    path: str
    state: str
    expected_size: int | None = None
    observed_size: int | None = None
    checksum_algorithm: str | None = None
    expected_checksum: str | None = None
    observed_checksum: str | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "state": self.state, "expected_size": self.expected_size,
                "observed_size": self.observed_size, "checksum_algorithm": self.checksum_algorithm,
                "expected_checksum": self.expected_checksum, "observed_checksum": self.observed_checksum,
                "content_type": self.content_type, "etag": self.etag,
                "last_modified": self.last_modified, "reason": self.reason, "metadata": self.metadata}


def verify_file(path: str | Path, *, expected_size: int | None = None,
                expected_checksum: str | None = None, algorithm: str = "sha256",
                content_type: str | None = None, etag: str | None = None,
                last_modified: str | None = None,
                observed_checksum: str | None = None) -> DownloadIntegrityReport:
    target = Path(path)
    if not target.is_file():
        return DownloadIntegrityReport(str(target), "corrupt", expected_size=expected_size,
                                       expected_checksum=expected_checksum, reason="output file is missing")
    observed_size = target.stat().st_size
    observed = None
    if expected_checksum:
        if observed_checksum:
            observed = str(observed_checksum).strip()
        else:
            digest = hashlib.new(algorithm)
            with target.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            observed = digest.hexdigest()
    # A surprising number of providers return a branded error page with a
    # successful HTTP status and no useful Content-Type.  Do a conservative
    # signature check, but allow intentionally requested HTML documents.
    html_target = target.suffix.lower() in {".html", ".htm", ".xhtml"}
    looks_like_html = False
    if not html_target and not (content_type or "").lower().startswith("text/"):
        try:
            with target.open("rb") as source:
                prefix = source.read(512).lstrip().lower()
            looks_like_html = prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
        except OSError:
            looks_like_html = False
    if expected_size is not None and observed_size != int(expected_size):
        state, reason = "corrupt", "observed size does not match expected size"
    elif expected_checksum and observed and observed.lower() != expected_checksum.lower():
        state, reason = "corrupt", "checksum mismatch"
    elif content_type and content_type.lower().startswith("text/html") and not html_target:
        state, reason = "provider_rejected", "provider returned HTML instead of a file"
    elif looks_like_html:
        state, reason = "provider_rejected", "provider returned an HTML error page instead of a file"
    elif expected_checksum:
        # A checksum matched (a mismatch was caught above): the CONTENT is proven.
        state, reason = "verified", None
    elif expected_size is not None:
        # Only the length was ever checked. Calling this "verified" claimed proof
        # we never had: a 2 GB volume with the right size and wrong bytes passed
        # as verified and was only caught later by the unrar CRC.
        state, reason = "size_verified", "length matched; no checksum published by the provider"
    else:
        state, reason = "unverifiable", None
    return DownloadIntegrityReport(str(target), state, expected_size, observed_size, algorithm,
                                  expected_checksum, observed, content_type, etag, last_modified, reason,
                                  {"media_type": mimetypes.guess_type(target.name)[0]})
