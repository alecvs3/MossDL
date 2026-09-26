"""Universal anti-bot HTTP client & clearance cache for Transfer Manager.

Provides TLS fingerprint camouflage (impersonating modern Chrome via curl_cffi),
domain-level cf_clearance cookie harvesting and reuse, and automatic fallback
to standard urllib.request if curl_cffi is unavailable.
"""

from __future__ import annotations

import collections
import html
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.client import HTTPMessage
from io import BytesIO
from typing import Any, Callable, Iterator, List, Optional, Union

try:
    from curl_cffi import requests as curl_requests
    _HAVE_CURL_CFFI = True
except ImportError:
    curl_requests = None
    _HAVE_CURL_CFFI = False

from . import route_http
from .telemetry import telemetry_bus
from .challenge_classifier import (
    ChallengeVerdict,
    ReplayInspectableStream,
    classify,
    is_inspectable_text,
    observe_received_response,
    response_observation,
)

logger = logging.getLogger(__name__)

DEFAULT_IMPERSONATE = "chrome131"
FALLBACK_IMPERSONATE = "chrome124"
DEFAULT_TIMEOUT = 20.0
MAX_RESPONSE_SIZE = 16 * 1024 * 1024  # 16 MB


@dataclass
class DomainClearance:
    """Cached domain clearance tokens (e.g. Cloudflare cf_clearance)."""
    hostname: str
    cookies: dict[str, str]
    user_agent: str
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 7200.0  # 2 hours default Cloudflare clearance TTL
    # The route it was earned on ("direct" or the proxy URL): clearance is bound
    # to the address that earned it (challenge_artifacts.reuse_decision).
    route: str = "direct"

    def is_valid(self) -> bool:
        return (time.time() - self.created_at) < self.ttl_seconds


class ClearanceCache:
    """Thread-safe cache of anti-bot clearance cookies indexed by domain."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, DomainClearance] = {}

    def set_clearance(self, url_or_domain: str, cookies: dict[str, str], user_agent: str, ttl: float = 7200.0) -> None:
        domain = self._normalize_domain(url_or_domain)
        if not domain:
            return
        with self._lock:
            self._cache[domain] = DomainClearance(
                hostname=domain,
                cookies=dict(cookies),
                user_agent=user_agent,
                created_at=time.time(),
                ttl_seconds=ttl,
                route=_current_route(),
            )
            telemetry_bus.record(
                level="INFO",
                subsystem="engine:http_client",
                message=f"[CLEARANCE_CACHED] Saved clearance cookies for domain '{domain}' ({len(cookies)} cookies, ttl={ttl}s)",
                context={"domain": domain, "cookie_names": list(cookies.keys())},
                tier="engine",
            )

    def get_clearance(self, url_or_domain: str, user_agent: str | None = None) -> Optional[DomainClearance]:
        """Clearance for the domain, only if the current request shares its identity."""
        from .challenge_artifacts import reuse_decision
        domain = self._normalize_domain(url_or_domain)
        if not domain:
            return None
        with self._lock:
            entry = self._cache.get(domain)
            if not entry:
                parts = domain.split(".")
                if len(parts) > 2:
                    parent = ".".join(parts[-2:])
                    entry = self._cache.get(parent)
            if entry and entry.is_valid():
                ok, reason = reuse_decision({"route": entry.route, "user_agent": entry.user_agent},
                                            {"route": _current_route(), "user_agent": user_agent})
                if not ok:
                    telemetry_bus.record(level="INFO", subsystem="engine:http_client",
                                         message=f"[CLEARANCE_NOT_REUSED] {domain}: {reason}",
                                         context={"domain": domain, "reason": reason}, tier="engine")
                    return None
                return entry
            if entry and not entry.is_valid():
                del self._cache[domain]
            return None

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def quarantine(self, url_or_domain: str) -> None:
        """Drop the domain's clearance after the site rejected an answer that used it."""
        domain = self._normalize_domain(url_or_domain)
        with self._lock:
            dropped = [d for d in self._cache if d == domain or d.endswith("." + domain) or domain.endswith("." + d)]
            for d in dropped:
                del self._cache[d]
        if dropped:
            telemetry_bus.record(level="WARN", subsystem="engine:http_client",
                                 message=f"[CLEARANCE_QUARANTINED] {domain}: rejected by the site",
                                 context={"domain": domain, "dropped": dropped}, tier="engine")

    @staticmethod
    def _normalize_domain(url_or_domain: str) -> str:
        if not url_or_domain:
            return ""
        if "://" in url_or_domain:
            parsed = urllib.parse.urlsplit(url_or_domain)
            return (parsed.hostname or "").lower()
        return url_or_domain.split(":")[0].strip().lower()


def _current_route() -> str:
    """The route this request goes out on (the bound task route, else the active one)."""
    try:
        return route_http.active_proxy() or "direct"
    except route_http.RouteUnavailable:
        return "blocked"


clearance_cache = ClearanceCache()


# Persistent curl_cffi sessions keyed by impersonation profile. Reusing a
# session amortizes TCP/TLS handshakes and preserves Set-Cookie state across a
# hoster's landing/step-1/step-2 sequence (previously each request recreated a
# fresh session, discarding challenge cookies and forcing repeated solves).
#
# Stickiness must not extend ACROSS tasks. A hoster like DataNodes registers the
# pending download server-side against the session cookie and then serves it from
# one shared URL, so two parts posting step one into the same jar overwrite each
# other and at most one of them can still fetch its own file. Callers driving a
# per-file flow pass `session_key` (the task id) to get a private jar; everything
# else keeps sharing the default one.
_SESSION_POOL: "collections.OrderedDict[tuple[str, str], Any]" = collections.OrderedDict()
_SESSION_LOCK = threading.Lock()

_SHARED_SESSION_KEY = ""

# Per-key jars are evicted least-recently-used so a long-lived engine cannot
# accumulate one session per file it has ever touched. The shared jar is never
# evicted. An evicted flow simply re-solves; it does not fail.
_MAX_KEYED_SESSIONS = 64


def _get_session(impersonate: str, session_key: str = _SHARED_SESSION_KEY) -> Any:
    if not _HAVE_CURL_CFFI:
        return None
    pool_key = (impersonate, session_key or _SHARED_SESSION_KEY)
    with _SESSION_LOCK:
        session = _SESSION_POOL.get(pool_key)
        if session is None:
            try:
                session = curl_requests.Session(impersonate=impersonate)
            except Exception:
                session = curl_requests.Session()
            _SESSION_POOL[pool_key] = session
        _SESSION_POOL.move_to_end(pool_key)
        _evict_keyed_sessions_locked()
        return session


def _evict_keyed_sessions_locked() -> None:
    keyed = [k for k in _SESSION_POOL if k[1] != _SHARED_SESSION_KEY]
    for key in keyed[: max(0, len(keyed) - _MAX_KEYED_SESSIONS)]:
        session = _SESSION_POOL.pop(key, None)
        try:
            if session is not None:
                session.close()
        except Exception:
            pass


def drop_session(session_key: str) -> None:
    """Discard every session held for `session_key` (call when a task finishes).

    Without this a long-lived engine accumulates one curl session per task.
    """
    if not session_key:
        return
    with _SESSION_LOCK:
        doomed = [k for k in _SESSION_POOL if k[1] == session_key]
        for key in doomed:
            session = _SESSION_POOL.pop(key, None)
            try:
                if session is not None:
                    session.close()
            except Exception:
                pass


def close_sessions() -> None:
    """Close the persistent session pool (idempotent; used on engine shutdown)."""
    with _SESSION_LOCK:
        for session in _SESSION_POOL.values():
            try:
                session.close()
            except Exception:
                pass
        _SESSION_POOL.clear()


class HttpResponseAdapter:
    """
    Adapter mimicking urllib.response / http.client response for seamless
    drop-in compatibility across existing engine downloaders and providers.
    """

    def __init__(self, content: bytes, status_code: int, url: str, headers: dict[str, str]) -> None:
        self._content = content
        self._status = status_code
        self._url = url
        self._io = BytesIO(content)
        self.code = status_code
        self.status = status_code

        self.headers = HTTPMessage()
        for k, v in headers.items():
            self.headers[k] = v
        prefix = content[:65536] if is_inspectable_text(headers) else b""
        self.challenge_verdict: ChallengeVerdict = observe_received_response(
            source="http_client.response", status=status_code, headers=headers, final_url=url, body_prefix=prefix,
        )

    def read(self, amt: Optional[int] = None) -> bytes:
        return self._io.read(amt)

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self._status

    def close(self) -> None:
        self._io.close()

    def __enter__(self) -> "HttpResponseAdapter":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class HttpStreamAdapter:
    """Stream adapter for chunked downloading with iter_content."""

    def __init__(self, raw_resp: Any, url: str) -> None:
        self._resp = raw_resp
        self._url = url
        self.status = getattr(raw_resp, "status_code", 200)
        self.code = self.status
        self._iter: Optional[Iterator[bytes]] = None
        self._buffer = bytearray()

        self.headers = HTTPMessage()
        raw_headers = getattr(raw_resp, "headers", {})
        for k, v in raw_headers.items():
            self.headers[k] = v
        observation = response_observation(
            status=self.status, headers=raw_headers, final_url=url,
        )
        self._replay = ReplayInspectableStream(self._raw_chunks(), observation, is_inspectable_text(raw_headers))
        self.challenge_verdict: ChallengeVerdict | None = None

    def _raw_chunks(self) -> Iterator[bytes]:
        if hasattr(self._resp, "iter_content"):
            yield from self._resp.iter_content()
            return
        if hasattr(self._resp, "read"):
            while True:
                chunk = self._resp.read(65536)
                if not chunk:
                    return
                yield chunk

    def iter_content(self, chunk_size: int = 65536) -> Iterator[bytes]:
        for chunk in self._replay.iter_bytes():
            self.challenge_verdict = self._replay.verdict
            if chunk:
                yield chunk

    def read(self, amt: Optional[int] = None) -> bytes:
        result = self._replay.read(amt)
        self.challenge_verdict = self._replay.verdict
        return result

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        if hasattr(self._resp, "close"):
            self._resp.close()

    def __enter__(self) -> "HttpStreamAdapter":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def request(
    method: str,
    url: str,
    *,
    data: Optional[Union[bytes, dict[str, Any], str]] = None,
    json_data: Optional[Any] = None,
    headers: Optional[dict[str, str]] = None,
    cookies: Optional[dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_redirects: bool = True,
    stream: bool = False,
    impersonate: str = DEFAULT_IMPERSONATE,
    use_clearance: bool = True,
    session_key: str = _SHARED_SESSION_KEY,
    proxy: Any = route_http.ACTIVE,
) -> Union[HttpResponseAdapter, HttpStreamAdapter]:
    """
    Execute HTTP request using curl_cffi with TLS camouflage & clearance cache.
    Falls back to urllib if curl_cffi is missing or encounters a C-level failure.

    `proxy` defaults to the active route; transfers pass their own route's
    proxy (None for a kernel-routed route). Neither path ever goes around it.
    """
    method = method.upper()
    proxies = route_http.curl_proxies(proxy)
    req_headers = dict(headers or {})
    req_cookies = dict(cookies or {})

    clearance_hit = False
    if use_clearance:
        domain_clearance = clearance_cache.get_clearance(url, user_agent=req_headers.get("User-Agent"))
        if domain_clearance:
            clearance_hit = True
            for ck, cv in domain_clearance.cookies.items():
                if ck not in req_cookies:
                    req_cookies[ck] = cv
            if domain_clearance.user_agent and "User-Agent" not in req_headers:
                req_headers["User-Agent"] = domain_clearance.user_agent

    t0 = time.perf_counter()

    if _HAVE_CURL_CFFI:
        try:
            session = _get_session(impersonate, session_key)
            if session is None:
                raise RuntimeError("curl_cffi session unavailable")
            resp = session.request(
                method=method,
                url=url,
                data=data if json_data is None else None,
                json=json_data,
                headers=req_headers,
                cookies=req_cookies,
                timeout=timeout,
                allow_redirects=allow_redirects,
                stream=stream,
                impersonate=impersonate,
                proxies=proxies,
            )

            dur_ms = (time.perf_counter() - t0) * 1000.0
            telemetry_bus.record(
                level="DEBUG",
                subsystem="engine:http_client",
                message=f"[HTTP_REQUEST] {method} {url} -> {resp.status_code} ({dur_ms:.1f}ms, clearance_hit={clearance_hit})",
                context={
                    "method": method,
                    "url": url,
                    "status": resp.status_code,
                    "clearance_hit": clearance_hit,
                    "stream": stream,
                    "impersonate": impersonate,
                    "proxied": bool(proxies),
                },
                duration_ms=dur_ms,
                tier="engine",
            )

            if "set-cookie" in resp.headers:
                sc = resp.headers.get("set-cookie", "")
                if "cf_clearance=" in sc:
                    m = re.search(r"cf_clearance=([^;]+)", sc)
                    if m:
                        # Preserve any previously cached challenge cookies
                        # (__cf_bm, _cfuvid, ...) instead of replacing the jar
                        # with only cf_clearance, which re-triggers challenges.
                        prior = clearance_cache.get_clearance(url)
                        merged = dict(prior.cookies) if prior else {}
                        merged["cf_clearance"] = m.group(1)
                        clearance_cache.set_clearance(
                            url,
                            merged,
                            user_agent=req_headers.get("User-Agent", ""),
                        )

            if stream:
                return HttpStreamAdapter(resp, resp.url or url)
            return HttpResponseAdapter(resp.content, resp.status_code, resp.url or url, dict(resp.headers))

        except Exception as exc:
            telemetry_bus.record(
                level="WARN",
                subsystem="engine:http_client",
                message=f"[HTTP_CURL_FALLBACK] curl_cffi request failed ({exc}); attempting urllib fallback",
                context={"method": method, "url": url, "error": str(exc)},
                tier="engine",
            )

    return _urllib_fallback(
        method=method,
        url=url,
        data=data,
        json_data=json_data,
        headers=req_headers,
        cookies=req_cookies,
        timeout=timeout,
        stream=stream,
        proxy=proxies["https"] if proxies else None,
    )


def _urllib_fallback(
    method: str,
    url: str,
    data: Optional[Union[bytes, dict[str, Any], str]],
    json_data: Optional[Any],
    headers: dict[str, str],
    cookies: dict[str, str],
    timeout: float,
    stream: bool,
    proxy: str | None,
) -> Union[HttpResponseAdapter, HttpStreamAdapter]:
    body_bytes: Optional[bytes] = None
    if json_data is not None:
        body_bytes = json.dumps(json_data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif isinstance(data, dict):
        body_bytes = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif isinstance(data, str):
        body_bytes = data.encode("utf-8")
    elif isinstance(data, bytes):
        body_bytes = data

    if cookies:
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers["Cookie"] = cookie_header

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    try:
        resp = route_http.urlopen(req, timeout=timeout, proxy=proxy)
        if stream:
            return HttpStreamAdapter(resp, resp.geturl() or url)
        body = resp.read()
        return HttpResponseAdapter(body, getattr(resp, "status", 200), resp.geturl() or url, dict(resp.headers))
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return HttpResponseAdapter(body, exc.code, exc.geturl() or url, dict(exc.headers or {}))


def get(url: str, **kwargs: Any) -> HttpResponseAdapter:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> HttpResponseAdapter:
    return request("POST", url, **kwargs)


def head(url: str, **kwargs: Any) -> HttpResponseAdapter:
    return request("HEAD", url, **kwargs)
