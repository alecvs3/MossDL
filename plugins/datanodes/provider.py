"""Primary DataNodes resolver owned by the DataNodes plugin.

The generic cyberdrop host collection remains a compatibility fallback in
``hooks.py``.  This path deliberately stops after step one when a Turnstile
token is required and gives the engine a resumable operation; it never starts a
second browser/provider pass itself.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from engine import http_client
from engine.critical_trace import mark, span
from engine.errors import NeedsUser, ProviderMappedError, ProviderUnavailable
from engine.models import ResolvedItem
from engine.providers.datanodes_resume import (
    CONTINUATION_KEY,
    build_continuation,
    resume,
    validate_continuation,
)
from engine.timer_detector import TimerDetector
from engine.timer_scheduler import timer_scheduler


class PrimaryFlowUnsupported(ProviderUnavailable):
    """The plugin could not recognize the host response; try legacy fallback."""


_OFFLINE = re.compile(
    r"(?i)(file\s+(?:not\s+found|was\s+deleted|has\s+been\s+(?:deleted|removed)|expired|unavailable)|"
    r"could\s+not\s+be\s+found|no\s+such\s+file|link\s+(?:is\s+)?dead)"
)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"


def _identity(url: str) -> tuple[str, str]:
    parts = [part for part in urllib.parse.urlsplit(url).path.split("/") if part]
    if not parts:
        raise ProviderUnavailable("Invalid DataNodes URL")
    if len(parts) >= 3 and parts[0] in {"d", "f"}:
        return parts[1], urllib.parse.unquote(parts[2]).replace("/", "_").replace("\\", "_")
    name = urllib.parse.unquote(parts[1]).replace("/", "_").replace("\\", "_") if len(parts) > 1 else f"datanodes_{parts[0]}.rar"
    return parts[0], name


def _headers(secrets: dict[str, Any]) -> dict[str, str]:
    headers = {"User-Agent": str(secrets.get("user_agent") or _UA), "Accept": "*/*"}
    supplied = secrets.get("headers")
    if isinstance(supplied, dict):
        headers.update({str(key): str(value) for key, value in supplied.items()})
    return headers


def _cookies(set_cookie: str) -> dict[str, str]:
    return {
        key: value
        for key, value in re.findall(r"(?:^|[,; ])\s*([A-Za-z0-9_-]+)=([^;]+)", set_cookie or "")
    }


def _item(url: str, code: str, name: str, direct: str, size: int | None,
          cookies: dict[str, str] | None = None) -> ResolvedItem:
    item = ResolvedItem(
        "datanodes", url, name, name, size=size, direct_url=direct,
        metadata={"type": "file"}, item_id=code,
    )
    item.cookies = dict(cookies or {})
    return item


def _size(text: str) -> int | None:
    match = re.search(r'data-scan-size=["\']([^"\']+)', text) or re.search(r'\(([\d.]+\s*[KMGT]?B)\)', text)
    if not match:
        return None
    parsed = re.search(r"([\d.]+)\s*([KMGT]?B|bytes)", match.group(1), re.I)
    if not parsed:
        return None
    multiplier = {"B": 1, "BYTES": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(float(parsed.group(1)) * multiplier.get(parsed.group(2).upper(), 1))


def _trace(task_id: str | None, message: str, level: str = "INFO", **context: Any) -> None:
    from engine.telemetry import telemetry_bus
    telemetry_bus.record(
        level=level, subsystem="engine:resolve",
        message=f"[{task_id[:8]}] [DN_PLUGIN] {message}" if task_id else f"[DN_PLUGIN] {message}",
        context={"task_id": task_id, **context}, tier="engine",
    )
    safe = {
        key: value for key, value in context.items()
        if not any(secret in key.lower() for secret in ("url", "token", "cookie", "rand", "sitekey"))
        and isinstance(value, (str, int, float, bool, type(None)))
    }
    mark(f"provider.datanodes.plugin.{message}", event="observation", **safe)


def _api_result(url: str, code: str, name: str, secrets: dict[str, Any]) -> ResolvedItem | None:
    key = str(secrets.get("api_key") or secrets.get("datanodes_api_key")
              or os.environ.get("MOON_DN_API_KEY") or os.environ.get("DATANODES_API_KEY") or "").strip()
    if not key:
        return None
    endpoint = f"https://datanodes.to/api/file/direct_link?file_code={code}&key={key}"
    try:
        with urllib.request.urlopen(urllib.request.Request(endpoint, headers=_headers(secrets)), timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        result = payload.get("result") if isinstance(payload, dict) else None
        if payload.get("status") == 200 and isinstance(result, dict) and result.get("url"):
            timer_scheduler.skip("datanodes.to", code, "api_key")
            return _item(url, code, name, str(result["url"]), result.get("size"))
    except Exception as exc:
        _trace(secrets.get("task_id"), "api_failed", level="WARN", error_type=type(exc).__name__)
    return None


def resolve(url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
    secrets = dict(secrets or {})
    code, name = _identity(url)
    task_id = secrets.get("task_id")

    continuation = secrets.get("_provider_continuation")
    if continuation is not None:
        valid, reason = validate_continuation(continuation, url=url, file_code=code)
        if valid:
            direct, cookies, size = resume(
                url=url, file_code=code, file_name=name, secrets=secrets,
                state=continuation,
                trace=lambda message, level="INFO", **ctx: _trace(task_id, message, level, **ctx),
            )
            return [_item(url, code, name, direct, size, cookies)]
        _trace(task_id, "continuation_invalid", level="WARN", reason=reason)

    if secrets.get("direct_url"):
        timer_scheduler.skip("datanodes.to", code, "direct_url")
        return [_item(url, code, name, str(secrets["direct_url"]), None, secrets.get("cookies"))]
    api_item = _api_result(url, code, name, secrets)
    if api_item:
        return [api_item]

    # DataNodes keys the pending download to the session, so every request in
    # this file's landing -> step-one -> step-two sequence needs a cookie jar of
    # its own. Sharing one across parts lets the last POST win and leaves the
    # other parts reading a sibling's page -- which presents as a missing `rand`
    # below, and so as a spurious PrimaryFlowUnsupported fallback to legacy.
    # Keyed by file code, not task id: the hoster's pending state is about the
    # file, so a retry of the same file keeps its cookie continuity while two
    # different files can never share a jar.
    jar = f"datanodes:{code}"

    with span("provider.datanodes.landing_get", resource="http", resource_id="datanodes.to"):
        response = http_client.get(url, headers=_headers(secrets), timeout=15, session_key=jar)
    body = response.read(2 * 1024 * 1024).decode("utf-8", "replace")
    visible = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", " ", body, flags=re.I | re.S)
    visible = re.sub(r"<a\b[^>]*>.*?</a>|<[^>]+>", " ", visible, flags=re.I | re.S)
    if response.status == 404 or _OFFLINE.search(visible):
        raise ProviderMappedError("DataNodes: File not found or deleted", "not_found", 404)
    if response.status >= 400:
        raise ProviderUnavailable(f"DataNodes landing request failed with HTTP {response.status}")
    cookies = _cookies(response.headers.get("set-cookie", "") if response.headers else "")
    discovered_size = _size(body)

    is_step1 = any(marker in body for marker in ('name="op" value="download1"', 'id="method_free"', 'name="method_free"'))
    if is_step1:
        candidates = re.findall(r'name=["\']fname["\']\s+value=["\']([^"\']+)', body)
        real_names = [candidate for candidate in candidates if candidate.strip().lower() not in {"", "download"}]
        form_name = name if name in candidates else (real_names[-1] if real_names else name)
        rand_match = re.search(r'name=["\']rand["\']\s+value=["\']([^"\']+)', body)
        form = {
            "op": "download1", "usr_login": "", "id": code, "fname": form_name,
            "referer": "", "method_free": "Free Download >>",
        }
        if rand_match:
            form["rand"] = rand_match.group(1)
        cookie_map = {"lang": "english", "file_name": form_name, "file_code": code, **cookies}
        step_headers = {
            **_headers(secrets), "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "; ".join(f"{key}={value}" for key, value in cookie_map.items()),
            "Origin": "https://datanodes.to", "Referer": url, "Host": "datanodes.to",
        }
        with span("provider.datanodes.step1_post", resource="http", resource_id="datanodes.to"):
            response = http_client.post("https://datanodes.to/download", data=form, headers=step_headers,
                                        timeout=15, session_key=jar)
        body = response.read(2 * 1024 * 1024).decode("utf-8", "replace")
        cookies.update(_cookies(response.headers.get("set-cookie", "") if response.headers else ""))
        if response.status >= 400:
            raise ProviderUnavailable(f"DataNodes step one failed with HTTP {response.status}")

    rand_match = re.search(r'rand=["\']([^"\']+)', body)
    dl_match = re.search(r'dl-token=["\']([^"\']+)', body)
    key_match = re.search(r'data-sitekey=["\']([^"\']+)', body) or re.search(r'sitekey:\s*["\']([^"\']+)', body)
    if not rand_match:
        raise PrimaryFlowUnsupported("DataNodes plugin could not identify the step-two form")
    timer = TimerDetector.best_candidate(TimerDetector.detect_candidates(body))
    countdown = float(timer.seconds if timer is not None else 10)
    site_key = key_match.group(1) if key_match else "0x4AAAAAAD8U9nktqncPIkBM"
    state = build_continuation(
        url=url, file_code=code, file_name=name, rand=rand_match.group(1),
        dl_token=dl_match.group(1) if dl_match else "", cookies=cookies,
        countdown_seconds=countdown, site_key=site_key, discovered_size=discovered_size,
    )
    token = str(secrets.get("turnstile_token") or secrets.get("cf-turnstile-response") or "")
    if not token:
        raise NeedsUser("DataNodes requires Cloudflare Turnstile verification", "turnstile", {
            "url": url, "host": "datanodes.to", "page_url": url,
            "site_key": site_key, "cookies": {"file_code": code},
            CONTINUATION_KEY: state,
        })
    direct, final_cookies, final_size = resume(
        url=url, file_code=code, file_name=name, secrets=secrets, state=state,
        trace=lambda message, level="INFO", **ctx: _trace(task_id, message, level, **ctx),
    )
    return [_item(url, code, name, direct, final_size, final_cookies)]
