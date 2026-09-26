"""Browser-assisted request capture and safe candidate ranking.

The browser extension/native host may send many requests for one page.  This
module keeps the request shape provider-neutral and strips credential-bearing
headers before anything crosses the persistence boundary.
"""

from __future__ import annotations

import mimetypes
import os
import hashlib
from dataclasses import replace
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .file_classifier import classify_file
from .models import CaptureCandidate

_SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
_SAFE_HEADERS = {"accept", "accept-language", "content-type", "range", "referer", "user-agent"}
_SAFE_METHODS = {"GET", "HEAD"}
_SECRET_QUERY_KEYS = {"sig", "signature", "token", "expires", "expiry", "x-amz-signature",
                      "x-amz-credential", "x-amz-security-token", "auth", "authorization"}


def redacted_url(url: str) -> str:
    """Remove fragments and known ephemeral query values before persistence."""
    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key.lower() not in _SECRET_QUERY_KEYS]
    return urlunsplit((parsed.scheme.lower(), (parsed.netloc or "").lower(), parsed.path,
                       urlencode(query), ""))


def canonical_source(url: str) -> str:
    return redacted_url(url)


def source_fingerprint(url: str) -> str:
    return hashlib.sha256(canonical_source(url).encode("utf-8")).hexdigest()


def _filename(url: str) -> str | None:
    name = os.path.basename(urlsplit(url).path.rstrip("/"))
    return name or None


def normalize_candidate(value: dict[str, Any], *, source: str = "browser") -> CaptureCandidate:
    url = str(value.get("url", "")).strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https", "ftp"} or not parsed.hostname:
        raise ValueError("capture candidate URL must use http, https, or ftp")
    method = str(value.get("method", "GET")).upper()
    if method not in _SAFE_METHODS:
        raise ValueError("only GET and HEAD capture requests can be downloaded")
    raw_headers = value.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise ValueError("capture candidate headers must be an object")
    headers = {
        str(key): str(item)[:4096] for key, item in raw_headers.items()
        if str(key).lower() not in _SECRET_HEADERS and str(key).lower() in _SAFE_HEADERS
    }
    mime = value.get("mime") or value.get("mime_type")
    filename = value.get("filename") or value.get("display_name") or _filename(url)
    size = value.get("size")
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    confidence = max(0.0, min(1.0, float(value.get("confidence", 0.0) or 0.0)))
    if not mime and filename:
        mime = mimetypes.guess_type(filename)[0]
    page_context = value.get("page_context") if isinstance(value.get("page_context"), dict) else {}
    page_url = value.get("page_url") or page_context.get("page_url")
    safe_url = redacted_url(url)
    return CaptureCandidate(url=safe_url, method=method, headers=headers,
                            referrer=value.get("referrer") or value.get("referer"),
                            credential_ref=value.get("credential_ref"), mime=mime,
                            filename=filename, size=size, confidence=confidence,
                            source=source, request_id=value.get("request_id"),
                            canonical_source=canonical_source(safe_url),
                            source_fingerprint=source_fingerprint(safe_url),
                            page_url=page_url, page_origin=value.get("page_origin"),
                            session_ref=value.get("session_ref"),
                            ranking_inputs={"method": method, "mime": mime, "size": size,
                                            "filename": filename, "initial_confidence": confidence})


def rank_candidates(values: Iterable[dict[str, Any] | CaptureCandidate], *, page_url: str | None = None,
                    limit: int = 128) -> list[CaptureCandidate]:
    """Normalize, deduplicate, and rank captured requests for user inspection."""
    candidates: dict[tuple[str, str], CaptureCandidate] = {}
    page_host = (urlsplit(page_url).hostname or "").lower() if page_url else ""
    for value in values:
        candidate = value if isinstance(value, CaptureCandidate) else normalize_candidate(value)
        parsed = urlsplit(candidate.url)
        score = candidate.confidence
        if candidate.method == "GET":
            score += 0.10
        if candidate.mime and (candidate.mime.startswith(("video/", "audio/", "image/")) or
                               candidate.mime == "application/octet-stream"):
            score += 0.30
        if candidate.filename and candidate.filename != "download":
            score += 0.15
        if page_host and (parsed.hostname or "").lower() == page_host:
            score += 0.05
        key = (candidate.method, candidate.canonical_source or canonical_source(candidate.url))
        candidate = replace(candidate, confidence=max(0.0, min(1.0, score)))
        previous = candidates.get(key)
        if previous is None or candidate.confidence > previous.confidence:
            candidates[key] = candidate
    return sorted(candidates.values(), key=lambda item: (-item.confidence, item.url))[:max(1, limit)]


def public_candidate(candidate: CaptureCandidate) -> dict[str, Any]:
    """Return the durable/public representation; no cookies or auth headers."""
    value = candidate.to_dict()
    value["url"] = redacted_url(str(value.get("url", "")))
    value["canonical_source"] = canonical_source(value["url"])
    value["source_fingerprint"] = source_fingerprint(value["url"])
    value["headers"] = {key: item for key, item in candidate.headers.items()
                         if key.lower() not in _SECRET_HEADERS}
    value.pop("credential_ref", None)
    return value


# Hosts a page talks to that never serve the user's download: bot challenges,
# translation, analytics and fonts. Their requests are captured but not useful.
_INFRASTRUCTURE_HOSTS = (
    "challenges.cloudflare.com", "translate.googleapis.com", "translate.google.com",
    "googletagmanager.com", "google-analytics.com", "fonts.googleapis.com", "fonts.gstatic.com",
    "hcaptcha.com", "recaptcha.net", "gstatic.com", "cloudflareinsights.com",
)


def noise_reason(url: str) -> str | None:
    """Why a captured request is not a download, or None when it may be one."""
    from .dom_cleaner import is_ad_domain

    host = (urlsplit(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _INFRASTRUCTURE_HOSTS):
        return "site infrastructure"
    if "/recaptcha/" in url or "/cdn-cgi/challenge-platform/" in url:
        return "bot challenge"
    if is_ad_domain(url):
        return "ad network"
    return None


def with_file_kinds(batch: dict[str, Any], blocked: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """Tag each candidate with its file category and, when it is clearly not a
    download, why (appended `file_kind` / `noise` fields).

    Done when batches are read rather than stored, so batches captured before
    the fields existed are tagged too. `blocked` holds the URLs the ad filter
    lists block.
    """
    for candidate in batch.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if "file_kind" not in candidate:
            candidate["file_kind"] = classify_file(mime=candidate.get("mime"), filename=candidate.get("filename"),
                                                   source_url=candidate.get("url")).category
        if "noise" not in candidate:
            url = str(candidate.get("url") or "")
            # `blocked`: what the filter lists say, checked for the whole batch at once.
            candidate["noise"] = noise_reason(url) or ("ad or tracker (filter lists)" if url in blocked else None)
    return batch
