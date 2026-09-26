from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any
from . import route_http

logger = logging.getLogger(__name__)


class BypassVipClient:
    """
    HTTP client for the Bypass.vip / Bypass.city REST API protocol.
    Used as an optional cloud-based Tier 2 fallback for complex or dynamic shortlinks.
    """

    def __init__(
        self,
        base_url: str = "https://api.bypass.vip/bypass",
        api_key: str | None = None,
        timeout: float = 4.0,
        enabled: bool = True,
    ) -> None:
        self.base_url = base_url.strip()
        self.api_key = api_key.strip() if api_key else None
        self.timeout = timeout
        self.enabled = enabled

    def configure(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        enabled: bool | None = None,
        timeout: float | None = None,
    ) -> None:
        if base_url is not None:
            self.base_url = base_url.strip()
        if api_key is not None:
            self.api_key = api_key.strip() if api_key else None
        if enabled is not None:
            self.enabled = bool(enabled)
        if timeout is not None:
            self.timeout = float(timeout)

    def resolve(self, target_url: str) -> str | None:
        """
        Queries the bypass API to unshorten target_url.
        Returns the direct unshortened destination URL or None if unavailable.
        """
        if not self.enabled:
            return None

        clean_url = target_url.strip()
        if not clean_url:
            return None

        endpoint = f"{self.base_url}?url={urllib.parse.quote(clean_url, safe='')}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            req = urllib.request.Request(endpoint, headers=headers)
            with route_http.urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    logger.debug("Bypass.vip returned HTTP status %d", resp.status)
                    return None
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))

            if isinstance(data, dict):
                status = str(data.get("status", "")).lower()
                result = data.get("result") or data.get("destination") or data.get("url")
                if status == "success" and isinstance(result, str):
                    res_clean = result.strip()
                    if (
                        res_clean.startswith(("http://", "https://"))
                        and res_clean != clean_url
                        and not any(x in res_clean.lower() for x in ("discord", "t.me", "bypass.vip", "shut down"))
                    ):
                        return res_clean
                    logger.debug("Bypass.vip success payload was not a valid target URL: %s", res_clean[:120])
        except Exception as exc:
            logger.debug("Bypass.vip query failed for %s: %s", clean_url, exc)

        return None
