from __future__ import annotations

import asyncio
import json
import logging
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class FlareResolution:
    url: str
    status: str
    message: str
    solution_cookies: list[dict[str, Any]] = field(default_factory=list)
    user_agent: str = ""
    response_html: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def cf_clearance(self) -> str | None:
        for c in self.solution_cookies:
            if c.get("name") == "cf_clearance":
                return c.get("value")
        return None

    @property
    def turnstile_token(self) -> str | None:
        for c in self.solution_cookies:
            if c.get("name") in {"cf-turnstile-response", "turnstile_token"}:
                return c.get("value")
        return None

    def cookie_header(self) -> str:
        return "; ".join(f"{c['name']}={c['value']}" for c in self.solution_cookies if "name" in c and "value" in c)


class FlareSolverrClient:
    """
    Client for interacting with local or dockerized FlareSolverr microservices (default: http://127.0.0.1:8191).
    Provides native Cloudflare Turnstile & anti-bot bypass support without requiring
    Transfer Manager itself to bundle a heavy Chrome/Chromium installation.
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8191",
        timeout: float = 60.0,
        session_id: str | None = None,
        proxy: str | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.session_id = session_id
        self.proxy = proxy

    def is_available(self) -> bool:
        """Fast TCP socket ping to check if FlareSolverr is active (<1.0s)."""
        try:
            parsed = urllib.parse.urlsplit(self.endpoint)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except Exception:
            return False

    async def get_health(self) -> dict[str, Any] | None:
        """Queries /health or root welcome message."""
        def _call() -> dict[str, Any] | None:
            try:
                url = f"{self.endpoint}/health"
                req = urllib.request.Request(url, headers={"User-Agent": "TransferManager/1.0"})
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception:
                return None

        return await asyncio.to_thread(_call)

    async def create_session(self, session_id: str | None = None) -> str | None:
        """Creates an isolated browser session in FlareSolverr."""
        sid = session_id or self.session_id
        payload: dict[str, Any] = {"cmd": "sessions.create"}
        if sid:
            payload["session"] = sid
        if self.proxy:
            payload["proxy"] = {"url": self.proxy}

        res = await self._post_v1(payload)
        if res and res.get("status") == "ok":
            self.session_id = res.get("session") or sid
            return self.session_id
        return None

    async def destroy_session(self, session_id: str | None = None) -> bool:
        """Destroys an active session."""
        sid = session_id or self.session_id
        if not sid:
            return True
        res = await self._post_v1({"cmd": "sessions.destroy", "session": sid})
        if res and res.get("status") == "ok":
            if self.session_id == sid:
                self.session_id = None
            return True
        return False

    async def resolve(
        self,
        url: str,
        method: str = "GET",
        post_data: str | None = None,
        max_timeout_ms: int = 60000,
        cookies: list[dict[str, str]] | None = None,
    ) -> FlareResolution:
        """
        Submits request.get or request.post through FlareSolverr to bypass Turnstile / Cloudflare challenges.
        """
        cmd = "request.post" if method.upper() == "POST" else "request.get"
        payload: dict[str, Any] = {
            "cmd": cmd,
            "url": url,
            "maxTimeout": max_timeout_ms,
        }
        if self.session_id:
            payload["session"] = self.session_id
        if self.proxy:
            payload["proxy"] = {"url": self.proxy}
        if cookies:
            payload["cookies"] = cookies
        if post_data and cmd == "request.post":
            payload["postData"] = post_data

        res = await self._post_v1(payload)
        if not res:
            return FlareResolution(
                url=url,
                status="error",
                message="FlareSolverr endpoint unreachable or timed out.",
            )

        status = res.get("status", "error")
        message = res.get("message", "")
        solution = res.get("solution", {})

        return FlareResolution(
            url=solution.get("url", url),
            status=status,
            message=message,
            solution_cookies=solution.get("cookies", []),
            user_agent=solution.get("userAgent", ""),
            response_html=solution.get("response", ""),
            headers=solution.get("headers", {}),
        )

    async def _post_v1(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        def _do_post() -> dict[str, Any] | None:
            url = f"{self.endpoint}/v1"
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json", "User-Agent": "TransferManager/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            return await asyncio.to_thread(_do_post)
        except Exception as exc:
            logger.warning("FlareSolverr request error [%s]: %s", payload.get("cmd"), exc)
            return None
