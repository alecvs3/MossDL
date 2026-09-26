"""Resume the DataNodes step-two operation without replaying the landing flow."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any, Callable, Mapping

from ..errors import NeedsUser, ProviderMappedError, ProviderUnavailable


CONTINUATION_KEY = "_engine_continuation"
RESUME_SECRET_KEY = "_provider_continuation"
SCHEMA_VERSION = 1


def build_continuation(*, url: str, file_code: str, file_name: str, rand: str,
                       dl_token: str, cookies: Mapping[str, str], countdown_seconds: float,
                       site_key: str, discovered_size: int | None) -> dict[str, Any]:
    """Create engine-private state needed to continue at the final POST."""
    now_ns = time.time_ns()
    return {
        "schema": SCHEMA_VERSION,
        "provider": "datanodes",
        "source_url": url,
        "file_code": file_code,
        "file_name": file_name,
        "rand": rand,
        "dl_token": dl_token,
        "cookies": dict(cookies),
        "timer_deadline_unix_ns": now_ns + int(max(0.0, countdown_seconds) * 1_000_000_000),
        "expires_unix_ns": now_ns + 5 * 60 * 1_000_000_000,
        "site_key": site_key,
        "discovered_size": discovered_size,
    }


def _direct_url(body: str) -> str | None:
    stripped = body.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            payload = json.loads(stripped)
            candidate = payload.get("url") if isinstance(payload, dict) else None
            if candidate:
                return urllib.parse.unquote(str(candidate))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    match = re.search(r'https?://[a-zA-Z0-9_\-.]+\.(?:datanodes\.to|dlproxy\.uk)/[^\s"\'<>]+', body)
    return match.group(0) if match else None


def validate_continuation(state: Any, *, url: str, file_code: str) -> tuple[bool, str]:
    if not isinstance(state, dict):
        return False, "missing"
    if state.get("schema") != SCHEMA_VERSION or state.get("provider") != "datanodes":
        return False, "schema_or_provider"
    if state.get("source_url") != url or state.get("file_code") != file_code:
        return False, "source_mismatch"
    if int(state.get("expires_unix_ns") or 0) <= time.time_ns():
        return False, "expired"
    if not state.get("rand"):
        return False, "missing_rand"
    return True, "valid"


def resume(*, url: str, file_code: str, file_name: str, secrets: Mapping[str, Any],
           state: Mapping[str, Any], trace: Callable[..., None]) -> tuple[str, dict[str, str], int | None]:
    """Submit step two from saved form/session state and return its direct URL."""
    valid, reason = validate_continuation(state, url=url, file_code=file_code)
    if not valid:
        trace("continuation_invalid", level="WARN", reason=reason)
        raise ProviderUnavailable(f"DataNodes continuation is {reason.replace('_', ' ')}")

    from ..critical_trace import mark, span
    from ..timer_scheduler import timer_scheduler

    host = "datanodes.to"
    timer_ctx = {"task_id": secrets.get("task_id"), "host": host, "file_code": file_code}
    remaining = max(0.0, (int(state["timer_deadline_unix_ns"]) - time.time_ns()) / 1_000_000_000)
    if remaining:
        timer_scheduler.arm(host, file_code, remaining, source="provider_continuation",
                            confidence=1.0, telemetry_ctx=timer_ctx, reason="resumed_pre_post_wait")
        timer_scheduler.wait_for_sync(host, file_code, telemetry_ctx=timer_ctx)
    else:
        timer_scheduler.satisfied(host, file_code, "continuation_deadline_elapsed", telemetry_ctx=timer_ctx)

    saved_cookies = {str(k): str(v) for k, v in dict(state.get("cookies") or {}).items()}
    post_data = {
        "op": "download2", "id": file_code, "rand": str(state["rand"]),
        "referer": "", "method_free": "", "method_premium": "", "g_captch__a": "1",
        "cf-turnstile-response": str(secrets.get("turnstile_token") or secrets.get("cf-turnstile-response") or ""),
    }
    if state.get("dl_token"):
        post_data["dl_token"] = str(state["dl_token"])

    cookie_map = {"lang": "english", "file_name": file_name, "file_code": file_code, **saved_cookies}
    for source in (secrets.get("cookies"),):
        if isinstance(source, dict):
            cookie_map.update({str(k): str(v) for k, v in source.items()})
    # Browser lanes share one host context. Concurrent DataNodes pages can
    # overwrite these per-file cookies in that context, so solver-harvested
    # cookies must never replace the continuation's exact file identity.
    cookie_map.update({"lang": "english", "file_name": file_name, "file_code": file_code})
    if secrets.get("cf_clearance"):
        cookie_map["cf_clearance"] = str(secrets["cf_clearance"])
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": "; ".join(f"{key}={value}" for key, value in cookie_map.items()),
        "Host": host,
        "Origin": "https://datanodes.to",
        "Referer": "https://datanodes.to/download",
        "X-Dn-Dl": "1",
        "User-Agent": str(secrets.get("user_agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"),
    }

    from .. import http_client
    mark("provider.datanodes.continuation.accepted", event="state", resource="provider", resource_id=host,
         timer_remaining_seconds=remaining)
    with timer_scheduler.host_gate(host):
        with span("provider.datanodes.step2_request", resource="http", resource_id=host):
            # Same jar the landing/step-one sequence used for this file; the
            # pending download only exists inside that session.
            response = http_client.post("https://datanodes.to/download", data=post_data, headers=headers,
                                        timeout=15, session_key=f"datanodes:{file_code}")
        with span("provider.datanodes.step2_response", resource="http_body", resource_id=host,
                  status=int(response.status)):
            body = response.read(1024 * 1024).decode("utf-8", "replace")
    direct = _direct_url(body)
    mark("provider.datanodes.direct_link_capture", event="state", resource="provider", resource_id=host,
         status=int(response.status), body_bytes=len(body.encode("utf-8")), captured=bool(direct))
    trace("continuation_post_result", status=int(response.status), found_direct=bool(direct), body_len=len(body))
    if direct:
        return direct, cookie_map, state.get("discovered_size")
    if int(response.status) in {429, 509}:
        raise ProviderMappedError("DataNodes: resumed operation was rate limited", "rate_limited", int(response.status))
    if any(marker in body.lower() for marker in (
        "cf-turnstile", "challenges.cloudflare.com", "verify you are human",
        "download-countdown", 'id="method_free"', 'name="op" value="download1"',
    )):
        raise NeedsUser("DataNodes invalidated the resumed browser challenge", "turnstile", {
            "url": url, "host": host, "page_url": url, "site_key": state.get("site_key"),
            "response_status": int(response.status), "response_reason": "continuation_rejected",
            # The single-use CAPTCHA token was rejected, but the provider form
            # session is still resumable. Keep it engine-private for the next
            # freshly solved token instead of replaying landing + step one.
            CONTINUATION_KEY: dict(state),
        })
    raise ProviderUnavailable(f"DataNodes resumed step two returned no direct link (HTTP {response.status})")
