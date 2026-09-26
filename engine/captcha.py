from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import logging
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from . import route_http

logger = logging.getLogger(__name__)


_TOKEN_SOLUTION_KEYS = (
    "token",
    "cf-turnstile-response",
    "turnstile_token",
    "g-recaptcha-response",
    "h-captcha-response",
)
_SENSITIVE_SOLUTION_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "signature",
    "signed_url",
    "clearance",
)


def solution_token(solution: Any) -> str:
    """Return a CAPTCHA token without exposing or persisting it."""
    if isinstance(solution, str):
        return solution.strip()
    if isinstance(solution, dict):
        for key in _TOKEN_SOLUTION_KEYS:
            value = solution.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _public_solution(value: Any, key: str = "") -> Any:
    """Keep challenge inspection useful while removing credential material."""
    low = key.lower()
    if any(part in low for part in _SENSITIVE_SOLUTION_PARTS) or low in {"cookies", "response_html", "post_body"}:
        return "[redacted]"
    if isinstance(value, dict):
        return {str(name): _public_solution(item, str(name)) for name, item in value.items()
                if not any(part in str(name).lower() for part in _SENSITIVE_SOLUTION_PARTS)
                and str(name).lower() not in {"cookies", "response_html", "post_body"}}
    if isinstance(value, list):
        return [_public_solution(item, key) for item in value]
    return value


from . import challenge_lifecycle  # noqa: E402
from .challenge_artifacts import ANSWER_TEXT, REUSABLE_CLEARANCE, SINGLE_USE_TOKEN  # noqa: E402
from .challenge_routing import IMAGE_INPUTS, PAGE_INPUTS, SITEKEY_INPUTS, Health, SolverCapability  # noqa: E402


class CaptchaType(str, Enum):
    IMAGE_TEXT = "image_text"
    POSITIONAL_CLICK = "positional_click"
    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    HCAPTCHA = "hcaptcha"
    RECAPTCHA_AUDIO = "recaptcha_audio"
    HCAPTCHA_AUDIO = "hcaptcha_audio"
    TURNSTILE = "turnstile"
    BROWSER_SESSION = "browser_session"


class CaptchaStatus(str, Enum):
    PENDING = "pending"
    SOLVING = "solving"
    SOLVED = "solved"
    EXPIRED = "expired"
    SKIPPED = "skipped"
    FAILED = "failed"
    # Appended: an answer was received and is being checked by the site.
    VERIFYING = "verifying"


@dataclass
class CaptchaChallenge:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str | None = None
    provider_id: str = "generic"
    captcha_type: CaptchaType | str = CaptchaType.IMAGE_TEXT
    params: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 90.0
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    status: CaptchaStatus | str = CaptchaStatus.PENDING
    solution: dict[str, Any] | None = None
    solver_used: str | None = None
    error: str | None = None
    resolved_at: float | None = None
    retry_count: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.captcha_type, str):
            try:
                self.captcha_type = CaptchaType(self.captcha_type)
            except ValueError:
                pass
        if not self.expires_at:
            self.expires_at = self.created_at + self.timeout_seconds

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def time_remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "provider_id": self.provider_id,
            "captcha_type": self.captcha_type.value if isinstance(self.captcha_type, CaptchaType) else str(self.captcha_type),
            "params": self.params,
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "time_remaining": round(self.time_remaining(), 1),
            "status": self.status.value if isinstance(self.status, CaptchaStatus) else str(self.status),
            # Solutions are returned to the waiting worker in memory, but the
            # public challenge shape must never expose tokens or cookies.
            "solution": _public_solution(self.solution) if self.solution is not None else None,
            "solver_used": self.solver_used,
            "error": self.error,
            "resolved_at": self.resolved_at,
            "retry_count": self.retry_count,
            # Appended: the engine-owned lifecycle the UI renders and commands against.
            "lifecycle": challenge_lifecycle.projection(self),
        }


class CaptchaSolver:
    """Abstract base class for CAPTCHA solvers in the cascade."""

    def __init__(self, solver_id: str, name: str, priority: int = 100, enabled: bool = True) -> None:
        self.solver_id = solver_id
        self.name = name
        self.priority = priority
        self.enabled = enabled

    def can_solve(self, challenge: CaptchaChallenge) -> bool:
        raise NotImplementedError

    def capability(self) -> "SolverCapability | None":
        """What this solver answers and needs (challenge_routing). None: never routed."""
        return None

    def health(self) -> "Health":
        """Whether it can run right now; cheap, no network probe unless cached."""
        return Health(True)

    async def solve(self, challenge: CaptchaChallenge) -> dict[str, Any] | None:
        raise NotImplementedError

    def report_result(self, challenge: CaptchaChallenge, valid: bool) -> None:
        """Feedback loop for reporting successful or invalid solutions."""
        pass


class LocalOcrSolver(CaptchaSolver):
    """
    Tier 1: Fast, offline OCR and heuristic solver.
    Solves arithmetic text questions, structured regex patterns, and simple OCR tokens.
    """

    def __init__(self, solver_id: str = "local_ocr", priority: int = 10, enabled: bool = True,
                 min_confidence: float = 0.85) -> None:
        super().__init__(solver_id=solver_id, name="Local OCR & Heuristics", priority=priority, enabled=enabled)
        self.min_confidence = min_confidence

    def capability(self) -> SolverCapability:
        # OCR answers are measured, not trusted: shadow until an approved gate.
        return SolverCapability(frozenset({CaptchaType.IMAGE_TEXT.value}), "text", ANSWER_TEXT,
                                inputs={"*": (IMAGE_INPUTS,)}, production=False)

    def can_solve(self, challenge: CaptchaChallenge) -> bool:
        if not self.enabled:
            return False
        if challenge.captcha_type != CaptchaType.IMAGE_TEXT:
            return False
        return "text_prompt" in challenge.params or "image_data" in challenge.params or "image_url" in challenge.params or "raw_text" in challenge.params or "mock_solution" in challenge.params

    async def solve(self, challenge: CaptchaChallenge) -> dict[str, Any] | None:
        params = challenge.params
        text = params.get("text_prompt", "").strip()

        # 1. Evaluate math expressions (e.g., "What is 4 + 7?", "12 - 5 = ", "3 * 6")
        math_solution = self._solve_math(text)
        if math_solution is not None:
            return {"text": str(math_solution), "confidence": 1.0, "method": "math_eval"}

        # 2. Extract answer from known mock/test payload or encoded plain hints
        if "mock_solution" in params:
            return {"text": str(params["mock_solution"]), "confidence": 1.0, "method": "mock"}

        # 3. Simple character segmentation / ASCII OCR heuristics for base64 / raw text
        raw_text = params.get("raw_text")
        if raw_text:
            cleaned = re.sub(r"[^A-Za-z0-9]", "", raw_text)
            if len(cleaned) in (4, 5, 6):
                return {"text": cleaned, "confidence": 0.9, "method": "pattern_match"}

        return None

    def _solve_math(self, text: str) -> int | None:
        if not text:
            return None
        match = re.search(r"(\d+)\s*([\+\-\*\/xX])\s*(\d+)", text)
        if not match:
            return None
        left, op, right = match.groups()
        a, b = int(left), int(right)
        if op in ("+",):
            return a + b
        if op in ("-",):
            return a - b
        if op in ("*", "x", "X"):
            return a * b
        if op in ("/",) and b != 0:
            return a // b
        return None


class ThirdPartyApiSolver(CaptchaSolver):
    """
    Tier 2: External paid solver API (2Captcha, Anti-Captcha, CapMonster).
    Supports token-based CAPTCHA solving with polling, balance verification,
    and bad-solution reporting for balance refund.
    """

    def __init__(self, solver_id: str = "twocaptcha", service_type: str = "twocaptcha",
                 api_key: str = "", priority: int = 50, enabled: bool = False,
                 base_url: str | None = None) -> None:
        super().__init__(solver_id=solver_id, name=f"Cloud API ({service_type})", priority=priority, enabled=enabled)
        self.service_type = service_type
        self.api_key = api_key
        self.base_url = base_url or ("https://2captcha.com" if service_type == "twocaptcha" else "https://api.anti-captcha.com")
        self._external_task_ids: dict[str, str] = {}
        self.http_client: Callable[..., Any] | None = None  # Hook for test mocks

    def capability(self) -> SolverCapability:
        token_types = {CaptchaType.RECAPTCHA_V2, CaptchaType.RECAPTCHA_V3, CaptchaType.HCAPTCHA, CaptchaType.TURNSTILE}
        return SolverCapability(
            frozenset({CaptchaType.IMAGE_TEXT.value} | {t.value for t in token_types}), "token", SINGLE_USE_TOKEN,
            inputs={CaptchaType.IMAGE_TEXT.value: (IMAGE_INPUTS,), "*": (SITEKEY_INPUTS, PAGE_INPUTS)})

    def health(self) -> Health:
        return Health(True) if self.api_key else Health(False, "no API key saved")

    def can_solve(self, challenge: CaptchaChallenge) -> bool:
        if not self.enabled or not self.api_key:
            return False
        return challenge.captcha_type in {
            CaptchaType.IMAGE_TEXT,
            CaptchaType.RECAPTCHA_V2,
            CaptchaType.RECAPTCHA_V3,
            CaptchaType.HCAPTCHA,
            CaptchaType.TURNSTILE,
        }

    async def get_balance(self) -> float:
        """Fetch remaining account balance."""
        if not self.api_key:
            return 0.0
        if self.http_client:
            resp = self.http_client("get_balance", {"key": self.api_key})
            return float(resp.get("balance", 0.0))

        try:
            url = f"{self.base_url}/res.php?key={urllib.parse.quote(self.api_key)}&action=getbalance&json=1"
            req = urllib.request.Request(url, headers={"User-Agent": "TransferManager/1.0"})
            with route_http.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
                if data.get("status") == 1:
                    return float(data.get("request", 0.0))
        except Exception as exc:
            logger.warning("Failed to fetch balance for %s: %s", self.solver_id, exc)
        return 0.0

    async def solve(self, challenge: CaptchaChallenge) -> dict[str, Any] | None:
        if not self.api_key:
            return None

        # If http_client mock is provided:
        if self.http_client:
            res = self.http_client("solve", {"type": challenge.captcha_type, "params": challenge.params})
            if res and "solution" in res:
                self._external_task_ids[challenge.id] = res.get("external_id", "mock-ext-id")
                return res["solution"]
            return None

        try:
            # 1. Create task / in.php (blocking HTTP offloaded off the event loop)
            task_payload = self._build_create_task_payload(challenge)
            create_url = f"{self.base_url}/in.php"

            def _create_task() -> dict[str, Any]:
                req = urllib.request.Request(
                    create_url,
                    data=json.dumps(task_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "TransferManager/1.0"}
                )
                with route_http.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            data = await asyncio.to_thread(_create_task)
            if data.get("status") != 1:
                logger.warning("%s createTask rejected: %s", self.solver_id, data.get("request"))
                return None
            external_id = str(data.get("request"))
            self._external_task_ids[challenge.id] = external_id

            # 2. Poll for result / res.php
            poll_url = f"{self.base_url}/res.php?key={urllib.parse.quote(self.api_key)}&action=get&id={urllib.parse.quote(external_id)}&json=1"
            start_time = time.time()
            max_poll = min(60.0, challenge.time_remaining() - 5.0)

            def _poll() -> dict[str, Any]:
                poll_req = urllib.request.Request(poll_url, headers={"User-Agent": "TransferManager/1.0"})
                with route_http.urlopen(poll_req, timeout=10) as poll_resp:
                    return json.loads(poll_resp.read().decode("utf-8"))

            while time.time() - start_time < max_poll:
                await asyncio.sleep(5)
                res_data = await asyncio.to_thread(_poll)
                if res_data.get("status") == 1:
                    token = res_data.get("request")
                    return self._format_solution(challenge.captcha_type, token)
                if res_data.get("request") != "CAPCHA_NOT_READY":
                    logger.warning("%s error response: %s", self.solver_id, res_data.get("request"))
                    break
        except Exception as exc:
            logger.warning("%s solve exception: %s", self.solver_id, exc)

        return None

    def report_result(self, challenge: CaptchaChallenge, valid: bool) -> None:
        """Report bad token for balance refund (established 2Captcha/Anti-Captcha precedent)."""
        external_id = self._external_task_ids.pop(challenge.id, None)
        if not external_id or valid or not self.api_key:
            return

        if self.http_client:
            self.http_client("report_bad", {"external_id": external_id})
            return

        try:
            report_url = f"{self.base_url}/res.php?key={urllib.parse.quote(self.api_key)}&action=reportbad&id={urllib.parse.quote(external_id)}&json=1"
            req = urllib.request.Request(report_url, headers={"User-Agent": "TransferManager/1.0"})
            with route_http.urlopen(req, timeout=10):
                logger.info("Reported bad solution %s to %s for refund", external_id, self.solver_id)
        except Exception as exc:
            logger.warning("Failed reporting bad solution to %s: %s", self.solver_id, exc)

    def _build_create_task_payload(self, challenge: CaptchaChallenge) -> dict[str, Any]:
        params = challenge.params
        payload: dict[str, Any] = {"key": self.api_key, "json": 1}
        if challenge.captcha_type == CaptchaType.IMAGE_TEXT:
            payload["method"] = "base64"
            payload["body"] = params.get("image_base64", "")
        elif challenge.captcha_type in (CaptchaType.RECAPTCHA_V2, CaptchaType.RECAPTCHA_V3):
            payload["method"] = "userrecaptcha"
            payload["googlekey"] = params.get("site_key", "")
            payload["pageurl"] = params.get("page_url", "")
            if challenge.captcha_type == CaptchaType.RECAPTCHA_V3:
                payload["version"] = "v3"
                payload["action"] = params.get("action", "verify")
                payload["min_score"] = params.get("min_score", 0.3)
        elif challenge.captcha_type == CaptchaType.HCAPTCHA:
            payload["method"] = "hcaptcha"
            payload["sitekey"] = params.get("site_key", "")
            payload["pageurl"] = params.get("page_url", "")
        elif challenge.captcha_type == CaptchaType.TURNSTILE:
            payload["method"] = "turnstile"
            payload["sitekey"] = params.get("site_key", "")
            payload["pageurl"] = params.get("page_url", "")
            if "action" in params:
                payload["action"] = params["action"]
            if "cdata" in params:
                payload["data"] = params["cdata"]
        return payload

    def _format_solution(self, captcha_type: CaptchaType | str, raw_response: Any) -> dict[str, Any]:
        if captcha_type == CaptchaType.IMAGE_TEXT:
            return {"text": str(raw_response)}
        return {"token": str(raw_response)}


class FlareSolverrSolver(CaptchaSolver):
    """
    Automated Cloudflare Turnstile and anti-bot solver backed by FlareSolverr microservice.
    Provides fast, headless Turnstile token and cf_clearance resolution.
    """

    def __init__(self, endpoint: str = "http://127.0.0.1:8191", solver_id: str = "flaresolverr",
                 priority: int = 40, enabled: bool = True) -> None:
        super().__init__(solver_id=solver_id, name="FlareSolverr Anti-Bot & Turnstile Bypass",
                         priority=priority, enabled=enabled)
        from .flaresolverr_client import FlareSolverrClient
        self.endpoint = endpoint
        self.client = FlareSolverrClient(endpoint=endpoint)

    def capability(self) -> SolverCapability:
        return SolverCapability(frozenset({CaptchaType.TURNSTILE.value, CaptchaType.BROWSER_SESSION.value}),
                                "clearance", REUSABLE_CLEARANCE, inputs={"*": (PAGE_INPUTS,)})

    def health(self) -> Health:
        # FlareSolverr browses from its own machine: clearance it earns is bound to
        # that address, so it is only coherent for tasks on the direct route.
        from . import route_http
        try:
            if route_http.active_proxy():
                return Health(False, "runs outside the selected route")
        except route_http.RouteUnavailable as exc:
            return Health(False, str(exc))
        return Health(True) if self.client.is_available() else Health(False, f"not reachable at {self.endpoint}")

    def can_solve(self, challenge: CaptchaChallenge) -> bool:
        if not self.enabled:
            return False
        if challenge.captcha_type not in {CaptchaType.TURNSTILE, CaptchaType.BROWSER_SESSION}:
            return False
        available = self.client.is_available()
        if not available:
            logger.info("FlareSolverr is offline or unreachable at %s; bypassing solver for %s", self.endpoint, challenge.id)
        return available

    async def solve(self, challenge: CaptchaChallenge) -> dict[str, Any] | None:
        page_url = challenge.params.get("url") or challenge.params.get("page_url")
        if not page_url:
            return None
        try:
            resolution = await self.client.resolve(page_url, method="GET", max_timeout_ms=int(challenge.timeout_seconds * 1000))
            if resolution.status == "ok":
                res_cookies = {c.get("name"): c.get("value") for c in resolution.solution_cookies}
                cf_clearance = resolution.cf_clearance or res_cookies.get("cf_clearance")
                token_match = re.search(r'name=["\']cf-turnstile-response["\'][^>]*value=["\']([^"\']+)["\']', resolution.response_html)
                token = token_match.group(1) if token_match else resolution.turnstile_token
                if not token and challenge.captcha_type == CaptchaType.BROWSER_SESSION and cf_clearance:
                    token = cf_clearance

                if not token:
                    logger.info("FlareSolverr bypassed Cloudflare edge but found no Turnstile token on [%s]", challenge.id)
                    return None

                return {
                    "token": token,
                    "cf-turnstile-response": token,
                    "turnstile_token": token,
                    "cf_clearance": cf_clearance,
                    "cookies": res_cookies,
                    "user_agent": resolution.user_agent,
                    "response_html": resolution.response_html,
                }
        except Exception as exc:
            logger.warning("FlareSolverr resolution error on challenge [%s]: %s", challenge.id, exc)
        return None


class _LoopbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for local loopback CAPTCHA webview and submission."""

    server_ref: BrowserLoopbackSolver

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "captcha":
            challenge_id = parts[1]
            challenge = self.server_ref.manager.get_challenge(challenge_id)
            if not challenge:
                self.send_error(404, "CAPTCHA challenge not found")
                return
            key = urllib.parse.parse_qs(parsed.query).get("k", [""])[0]
            refused = self.server_ref.check_key(challenge_id, key, consume=False)
            if refused:
                self.send_error(403, f"This captcha link is {refused}; open it again from the app")
                return

            html_content = self.server_ref.render_challenge_html(challenge)
            data = html_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "captcha" and parts[2] == "submit":
            challenge_id = parts[1]
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                if content_len > 16 * 1024:
                    self.send_error(413, "Answer too large")
                    return
                body = self.rfile.read(content_len).decode("utf-8")
                payload = json.loads(body) if body else {}
                token = payload.get("token")
                refused = self.server_ref.check_key(challenge_id, str(payload.get("key") or ""), consume=True)
                if refused is None and not (isinstance(token, str) and 0 < len(token) <= 8192):
                    refused = "invalid"
                success = refused is None and self.server_ref.manager.solve_challenge(
                    challenge_id=challenge_id, solution={"token": token}, solver_id="browser_loopback")
                response_body = json.dumps({"success": success, "outcome": refused or ("accepted" if success else "denied")}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
                return
            except Exception as exc:
                self.send_error(400, f"Submission error: {exc}")
                return

        self.send_error(404, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        pass


class AutomatedBrowserSolver(CaptchaSolver):
    """
    Automated Headless Browser Solver (Clearcote + Camoufox/Patchright fallback).
    Navigates to challenge URL, solves Turnstile/Cloudflare interstitial challenges in ~1.9s,
    and harvests clearance cookies directly into engine.http_client.
    """

    def __init__(self, solver_id: str = "automated_browser", priority: int = 35, enabled: bool = True) -> None:
        super().__init__(solver_id=solver_id, name="Automated Headless Browser (Clearcote / Camoufox)", priority=priority, enabled=enabled)

    def capability(self) -> SolverCapability:
        types = {CaptchaType.TURNSTILE, CaptchaType.BROWSER_SESSION, CaptchaType.RECAPTCHA_V2,
                 CaptchaType.RECAPTCHA_V3, CaptchaType.HCAPTCHA}
        # Runs on the task's own route (browser_solver binds it), so its clearance is coherent.
        return SolverCapability(frozenset(t.value for t in types), "clearance", REUSABLE_CLEARANCE,
                                inputs={"*": (PAGE_INPUTS,)}, identity_bound=True)

    def health(self) -> Health:
        from .clearcote_manager import get_clearcote_executable
        exe = get_clearcote_executable()
        if exe and exe.exists():
            return Health(True)
        chrome = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")]
        if any(os.path.exists(path) for path in chrome):
            return Health(True)
        try:
            import camoufox  # noqa: F401
            return Health(True)
        except ImportError:
            return Health(False, "no browser installed (Clearcote, Camoufox or Chrome)")

    def can_solve(self, challenge: CaptchaChallenge) -> bool:
        if not self.enabled:
            return False
        # Solves Turnstile, Cloudflare interstitial, and URL-based protection challenges
        if challenge.captcha_type == CaptchaType.TURNSTILE:
            return True
        params = challenge.params or {}
        return bool(params.get("page_url") or params.get("url"))

    async def solve(self, challenge: CaptchaChallenge) -> dict[str, Any] | None:
        from .browser_solver import solver_daemon
        params = challenge.params or {}
        target_url = params.get("page_url") or params.get("url")
        if not target_url:
            return None

        initial_cookies = params.get("cookies")
        if not isinstance(initial_cookies, dict):
            initial_cookies = None

        # Execute in thread to keep asyncio event loop responsive
        # A browser solver must yield to the manual path before the durable
        # challenge expires.  Keeping the full 90s challenge lifetime here
        # made a stalled headless Turnstile indistinguishable from a dead UI.
        browser_timeout = min(float(challenge.timeout_seconds or 35.0), 45.0)
        result = await asyncio.to_thread(solver_daemon.solve_challenge_sync, target_url, browser_timeout, initial_cookies, challenge.task_id)
        if result.get("success"):
            cookies = result.get("cookies", {})
            token_val = result.get("turnstile_token") or cookies.get("cf_clearance") or "cleared"
            solution: dict[str, Any] = {
                "token": token_val,
                "cf-turnstile-response": token_val,
                "turnstile_token": result.get("turnstile_token"),
                "cf_clearance": cookies.get("cf_clearance"),
                "cookies": cookies,
                "engine": result.get("engine"),
                "title": result.get("title"),
            }
            # Propagate direct download URL if captured via response interception or DOM
            if result.get("direct_url"):
                solution["direct_url"] = result["direct_url"]
                solution["direct_link"] = result["direct_url"]
                solution["link"] = result["direct_url"]
            return solution
        return None


class BrowserLoopbackSolver(CaptchaSolver):
    """
    Tier 3: Local HTTP Loopback Bridge.
    Serves isolated HTML pages for Cloudflare Turnstile, reCAPTCHA v2/v3, and hCaptcha,
    capturing completed tokens via loopback webhook.
    """

    def __init__(self, manager: CaptchaManager, solver_id: str = "browser_loopback",
                 priority: int = 70, enabled: bool = True, host: str = "127.0.0.1", port: int = 0,
                 auto_open: bool = True) -> None:
        super().__init__(solver_id=solver_id, name="Browser Loopback Bridge (Turnstile / hCaptcha / reCAPTCHA)", priority=priority, enabled=enabled)
        self.manager = manager
        self.host = host
        self.port = port
        self.auto_open = auto_open
        self.httpd: HTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._opened_challenge_ids: set[str] = set()
        # challenge id -> (key digest, generation, expires): one live key per challenge.
        self._keys: dict[str, tuple[str, int, float]] = {}
        self._keys_lock = threading.Lock()
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.httpd is not None:
                return

            class CustomHandler(_LoopbackHandler):
                server_ref = self

            self.httpd = HTTPServer((self.host, self.port), CustomHandler)
            self.port = self.httpd.server_port
            self._server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self._server_thread.start()
            logger.info("Captcha Browser Loopback running on http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        with self._lock:
            if self.httpd:
                self.httpd.shutdown()
                self.httpd.server_close()
                self.httpd = None
                self._server_thread = None

    def auto_open_browser(self, challenge_id: str, url: str | None = None) -> None:
        if not self.auto_open or challenge_id in self._opened_challenge_ids:
            return
        self._opened_challenge_ids.add(challenge_id)
        url = url or self.get_url(challenge_id)
        from .telemetry import telemetry_bus
        # The key in the URL is a secret: logs get the address without it.
        public = url.split("?", 1)[0]
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:captcha",
            message=f"[CAPTCHA_AUTO_OPEN] Spawning default browser for loopback URL: {public}",
            context={"challenge_id": challenge_id, "url": public},
            tier="engine",
        )
        def _open() -> None:
            try:
                import webbrowser
                logger.info("Auto-opening captcha loopback URL in browser: %s", public)
                webbrowser.open(url)
            except Exception as e:
                logger.warning("Failed to auto-open browser for captcha %s: %s", challenge_id, e)
        threading.Thread(target=_open, daemon=True).start()

    def get_url(self, challenge_id: str, auto_open: bool = False) -> str:
        """The helper page's address, with a fresh single-use key for this attempt."""
        self.start()
        key = secrets.token_urlsafe(24)
        challenge = self.manager.get_challenge(challenge_id)
        generation = challenge_lifecycle.generation_of(challenge) if challenge else 0
        with self._keys_lock:
            self._keys[challenge_id] = (hashlib.sha256(key.encode()).hexdigest(), generation, time.time() + 15 * 60)
        url = f"http://{self.host}:{self.port}/captcha/{challenge_id}?k={key}"
        if auto_open:
            self.auto_open_browser(challenge_id, url)
        return url

    def check_key(self, challenge_id: str, key: str, *, consume: bool) -> str | None:
        """None if the key opens this challenge's current attempt, else the outcome."""
        with self._keys_lock:
            entry = self._keys.get(challenge_id)
            if entry is None or not key or hashlib.sha256(key.encode()).hexdigest() != entry[0]:
                return "denied"
            if time.time() > entry[2]:
                return "expired"
            challenge = self.manager.get_challenge(challenge_id)
            if challenge is None or challenge_lifecycle.generation_of(challenge) != entry[1]:
                return "stale"
            if consume:
                del self._keys[challenge_id]
        return None

    def capability(self) -> SolverCapability:
        types = {CaptchaType.RECAPTCHA_V2, CaptchaType.RECAPTCHA_V3, CaptchaType.HCAPTCHA}
        return SolverCapability(frozenset(t.value for t in types), "token", SINGLE_USE_TOKEN,
                                inputs={"*": (SITEKEY_INPUTS,)}, manual=True)

    def can_solve(self, challenge: CaptchaChallenge) -> bool:
        if not self.enabled:
            return False
        # Cloudflare Turnstile strictly validates Origin and Referer headers against the registered host domain.
        # It unconditionally rejects localhost (127.0.0.1) origins, so loopback solver must not handle it.
        return challenge.captcha_type in {
            CaptchaType.RECAPTCHA_V2,
            CaptchaType.RECAPTCHA_V3,
            CaptchaType.HCAPTCHA,
        }

    async def solve(self, challenge: CaptchaChallenge) -> dict[str, Any] | None:
        # Loopback is driven by client/browser interaction.
        # Ensure the server is running and auto-open default browser if enabled.
        self.start()
        from .telemetry import telemetry_bus
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:captcha",
            message=f"[CAPTCHA_LOOPBACK_READY] Browser loopback server active on port {self.port} for challenge {challenge.id}",
            context={"challenge_id": challenge.id, "port": self.port, "auto_open": self.auto_open},
            tier="engine",
        )
        # Only auto-open if explicitly standalone (UI running manages opening to prevent double-tabs)
        if self.auto_open and not getattr(self, "ui_managed", False):
            self.auto_open_browser(challenge.id)
        return None

    def render_challenge_html(self, challenge: CaptchaChallenge) -> str:
        c_type = challenge.captcha_type
        params = challenge.params
        site_key = html.escape(params.get("site_key", "") or params.get("sitekey", ""))
        action = html.escape(params.get("action", "verify"))
        title = html.escape(f"Security Verification - {challenge.provider_id}")
        page_url = html.escape(str(params.get("page_url", "") or params.get("url", "")))

        widget_html = ""
        script_tag = ""

        if c_type == CaptchaType.TURNSTILE:
            script_tag = '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>'
            widget_html = f'<div class="cf-turnstile" data-sitekey="{site_key}" data-callback="onCaptchaSuccess"></div>'
            if page_url:
                widget_html += f'<div style="margin-top:14px; font-size:0.85rem;"><a href="{page_url}" target="_blank" style="color:#60a5fa; text-decoration:none;">Open original host site ({challenge.provider_id}) &rarr;</a></div>'
        elif c_type == CaptchaType.RECAPTCHA_V2:
            script_tag = '<script src="https://www.google.com/recaptcha/api.js" async defer></script>'
            widget_html = f'<div class="g-recaptcha" data-sitekey="{site_key}" data-callback="onCaptchaSuccess"></div>'
        elif c_type == CaptchaType.HCAPTCHA:
            script_tag = '<script src="https://js.hcaptcha.com/1/api.js" async defer></script>'
            widget_html = f'<div class="h-captcha" data-sitekey="{site_key}" data-callback="onCaptchaSuccess"></div>'

        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {script_tag}
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #111418;
      color: #e5e7eb;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      padding: 20px;
      box-sizing: border-box;
    }}
    .card {{
      background: #1e242b;
      border: 1px solid #374151;
      border-radius: 12px;
      padding: 32px;
      max-width: 480px;
      width: 100%;
      text-align: center;
      box-shadow: 0 8px 30px rgba(0,0,0,0.5);
    }}
    h2 {{ margin-top: 0; font-size: 1.25rem; font-weight: 600; color: #f9fafb; }}
    p {{ font-size: 0.9rem; color: #9ca3af; margin-bottom: 24px; }}
    .widget-container {{
      display: flex;
      flex-direction: column;
      align-items: center;
      margin: 20px 0;
      min-height: 75px;
    }}
    .status {{ font-size: 0.85rem; color: #3b82f6; margin-top: 16px; display: none; }}
    .status.success {{ color: #10b981; display: block; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Verification Required</h2>
    <p>Please complete the verification below for <strong>{challenge.provider_id}</strong> to resume your download.</p>
    <div class="widget-container">
      {widget_html}
    </div>
    <div id="status" class="status"></div>
  </div>
  <script>
    function onCaptchaSuccess(token) {{
      var statusEl = document.getElementById("status");
      statusEl.textContent = "Verifying solution with Transfer Manager...";
      statusEl.style.display = "block";
      fetch("/captcha/{challenge.id}/submit", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ token: token, key: new URLSearchParams(location.search).get("k") }})
      }}).then(function(res) {{
        return res.json();
      }}).then(function(data) {{
        if (data.success) {{
          statusEl.textContent = "Solved! You can close this window now.";
          statusEl.className = "status success";
          setTimeout(function() {{ window.close(); }}, 1500);
        }} else {{
          statusEl.textContent = "Failed to submit solution.";
        }}
      }}).catch(function(err) {{
        statusEl.textContent = "Network error: " + err.message;
      }});
    }}
  </script>
</body>
</html>"""


class InteractiveUiSolver(CaptchaSolver):
    """
    Tier 4: Desktop Interactive UI Solver.
    Prompts user via desktop dialog with countdown bar, audio alert, and single-dialog flood control.
    """

    def __init__(self, manager: CaptchaManager, solver_id: str = "interactive_ui",
                 priority: int = 100, enabled: bool = True) -> None:
        super().__init__(solver_id=solver_id, name="Interactive Desktop UI", priority=priority, enabled=enabled)
        self.manager = manager

    def capability(self) -> SolverCapability:
        return SolverCapability(frozenset(t.value for t in CaptchaType), "answer", SINGLE_USE_TOKEN, manual=True)

    def can_solve(self, challenge: CaptchaChallenge) -> bool:
        return self.enabled

    async def solve(self, challenge: CaptchaChallenge) -> dict[str, Any] | None:
        return await self.manager.enqueue_for_ui(challenge)


class CaptchaManager:
    """
    Central CAPTCHA Subsystem Manager (JDownloader 2 Precedent).
    Coordinates:
      - Multi-tiered solver cascade
      - Asynchronous task suspension & thread synchronization
      - Single-dialog flood-control queue
      - Timeout & countdown coordination
      - Solution verification feedback loop
      - Durable event outbox emission
      - Persistent store sync
    """

    def __init__(self, store: Any = None, event_publisher: Any = None) -> None:
        self.store = store
        self.events = event_publisher
        self._lock = threading.RLock()

        # In-memory challenges and completion futures
        self._challenges: dict[str, CaptchaChallenge] = {}
        self._futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._sync_events: dict[str, threading.Event] = {}

        # Flood control single-dialog queue
        self._active_ui_challenge_id: str | None = None
        self._ui_queue: list[str] = []

        # Solvers
        self.solvers: list[CaptchaSolver] = []
        self.loopback_solver = BrowserLoopbackSolver(self)
        self._register_default_solvers()
        # Settles answers by the site's verdict; the service hooks clearance sharing to it.
        from .challenge_verifier import ChallengeVerifier
        self.verifier = ChallengeVerifier(self)

        # Host skip policies (skip all for host until timestamp)
        self._host_skips: dict[str, float] = {}
        self._event_loop: Any = None

        # Optional engine hook receiving (task_id, coordinator_state, params)
        # so the service can map coordinator phases onto task lifecycle stages.
        self._coordinator_callback: Any = None
        self._manual_required_callback: Any = None

    def _complete_future(self, fut: Any, value: Any = None, exc: BaseException | None = None) -> None:
        """Thread-safe future completion (set_result/set_exception may be called
        from worker threads while the future belongs to the engine event loop)."""
        if fut is None or fut.done():
            return
        loop = self._event_loop
        try:
            if loop is not None and loop.is_running():
                if exc is not None:
                    loop.call_soon_threadsafe(fut.set_exception, exc)
                else:
                    loop.call_soon_threadsafe(fut.set_result, value)
            else:
                if exc is not None:
                    fut.set_exception(exc)
                else:
                    fut.set_result(value)
        except Exception as exc_:  # pragma: no cover - defensive
            logger.error("Failed completing CAPTCHA future: %s", exc_)

    def set_coordinator_callback(self, cb: Any) -> None:
        """Register a listener for durable CAPTCHA coordinator state changes."""
        self._coordinator_callback = cb

    def set_manual_required_callback(self, cb: Any) -> None:
        """Register a listener called once the automated cascade hands a challenge to the user."""
        self._manual_required_callback = cb

    def _register_default_solvers(self) -> None:
        from .darknet_solver import DarknetYoloSolver
        from .ddddocr_solver import DdddOcrSolver
        from .audio_solver import AudioChallengeSolver

        self.local_ocr = LocalOcrSolver()
        self.darknet_yolo = DarknetYoloSolver()
        self.ddddocr = DdddOcrSolver()
        self.audio_solver = AudioChallengeSolver()
        self.flaresolverr = FlareSolverrSolver()
        self.automated_browser = AutomatedBrowserSolver()
        self.twocaptcha = ThirdPartyApiSolver(solver_id="twocaptcha", service_type="twocaptcha")
        self.anticaptcha = ThirdPartyApiSolver(solver_id="anticaptcha", service_type="anticaptcha")
        self.capmonster = ThirdPartyApiSolver(solver_id="capmonster", service_type="capmonster")
        self.interactive_ui = InteractiveUiSolver(self)

        self.solvers = [
            self.local_ocr,
            self.darknet_yolo,
            self.ddddocr,
            self.audio_solver,
            self.flaresolverr,
            self.automated_browser,
            self.twocaptcha,
            self.anticaptcha,
            self.capmonster,
            self.loopback_solver,
            self.interactive_ui,
        ]
        self._sort_solvers()

    def _sort_solvers(self) -> None:
        self.solvers.sort(key=lambda s: s.priority)

    def recover_after_restart(self) -> int:
        """Attempts in flight when the engine stopped are handed back to the user,
        with a new generation so a click on the old attempt is refused as stale."""
        if not self.store or not hasattr(self.store, "list_captcha_challenges"):
            return 0
        recovered = 0
        for row in self.store.list_captcha_challenges():
            if str(row.get("status")) not in {"pending", "solving", "verifying"}:
                continue
            challenge = self.store.get_captcha_challenge(row["id"])
            if challenge is not None and challenge_lifecycle.recover_after_restart(challenge):
                challenge.status = CaptchaStatus.PENDING
                challenge.solution = None
                self._challenges[challenge.id] = challenge
                self._persist_challenge(challenge)
                recovered += 1
        return recovered

    def get_challenge(self, challenge_id: str) -> CaptchaChallenge | None:
        with self._lock:
            if challenge_id in self._challenges:
                return self._challenges[challenge_id]
            if self.store and hasattr(self.store, "get_captcha_challenge"):
                challenge = self.store.get_captcha_challenge(challenge_id)
                if challenge:
                    self._challenges[challenge.id] = challenge
                    return challenge
            return None

    def register_challenge(self, challenge: CaptchaChallenge) -> None:
        """Register a pending challenge without starting an automated solver.

        Service orchestration uses this for the package leader before it
        launches the asynchronous cascade. Registration itself is side-effect
        free; Clearcote/manual execution is owned by request_solution and the
        coordinator state remains durable across restart.
        """
        with self._lock:
            self._challenges[challenge.id] = challenge
            self._persist_challenge(challenge)

    def list_pending_challenges(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._cleanup_expired()
            if self.store and hasattr(self.store, "list_captcha_challenges"):
                # Rehydrate pending challenges after an engine restart so the
                # UI sees the durable queue, not only this process's memory.
                for row in self.store.list_captcha_challenges(task_id, pending_only=True):
                    if row["id"] not in self._challenges:
                        challenge = self.store.get_captcha_challenge(row["id"])
                        if challenge:
                            self._challenges[challenge.id] = challenge
            results = []
            for c in self._challenges.values():
                if c.status in (CaptchaStatus.PENDING, CaptchaStatus.SOLVING):
                    if task_id is None or c.task_id == task_id:
                        d = c.to_dict()
                        d["is_active_in_ui"] = (c.id == self._active_ui_challenge_id)
                        d["loopback_url"] = self.loopback_solver.get_url(c.id) if self.loopback_solver.can_solve(c) else None
                        results.append(d)
            return results

    def get_active_ui_challenge(self) -> dict[str, Any] | None:
        with self._lock:
            self._cleanup_expired()
            if self._active_ui_challenge_id and self._active_ui_challenge_id in self._challenges:
                c = self._challenges[self._active_ui_challenge_id]
                d = c.to_dict()
                d["is_active_in_ui"] = True
                d["loopback_url"] = self.loopback_solver.get_url(c.id) if self.loopback_solver.can_solve(c) else None
                return d
            return None

    async def request_solution(self, challenge: CaptchaChallenge, *, force_automated: bool = False) -> dict[str, Any]:
        """
        Main asynchronous entrypoint. Evaluates solver cascade:
        Local OCR -> 3rd-party Cloud API -> Browser Loopback -> Interactive Desktop UI.
        """
        with self._lock:
            if challenge.provider_id in self._host_skips:
                if time.time() < self._host_skips[challenge.provider_id]:
                    challenge.status = CaptchaStatus.SKIPPED
                    challenge.error = f"Host {challenge.provider_id} CAPTCHAs currently skipped by user"
                    self._persist_challenge(challenge)
                    raise TimeoutError(challenge.error)

            self._challenges[challenge.id] = challenge
            loop = asyncio.get_running_loop()
            self._event_loop = loop
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._futures[challenge.id] = future
            self._persist_challenge(challenge)

        challenge_lifecycle.advance(challenge, challenge_lifecycle.ROUTING, "solver cascade started")
        # Keep the engine-visible cascade phase separate from the generic
        # challenge status.  The task/package coordinator uses this field to
        # decide whether the user should see a single manual fallback action.
        challenge.params = {**(challenge.params or {}), "coordinator_state": "clearcote_pending"}
        self._persist_challenge(challenge)

        # 1. Cascade evaluation through registered solvers
        preferences = self.store.get_setting("captcha_ui_preferences", {}) if self.store else {}
        # Standalone manager instances are also used by resolver integrations
        # and tests without a persisted UI settings store. Preserve their
        # existing solver cascade; persisted application instances honor the
        # UI master switch.
        master_enabled = force_automated or self.store is None or bool(preferences.get("captchaMaster", False))
        enabled_by_type = {
            CaptchaType.HCAPTCHA: bool(preferences.get("captchaHcaptcha", True)),
            CaptchaType.RECAPTCHA_V2: bool(preferences.get("captchaRecaptcha", True)),
            CaptchaType.RECAPTCHA_V3: bool(preferences.get("captchaRecaptcha", True)),
            CaptchaType.POSITIONAL_CLICK: bool(preferences.get("captchaPositional", False)),
        }
        from .telemetry import telemetry_bus
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:captcha",
            message=f"[CAPTCHA_CASCADE_START] Evaluating solvers for {challenge.captcha_type} challenge on {challenge.provider_id} (id={challenge.id}, master_enabled={master_enabled})",
            context={"challenge_id": challenge.id, "type": str(challenge.captcha_type), "provider": challenge.provider_id, "master_enabled": master_enabled},
            tier="engine",
        )
        from .challenge_routing import route
        plan = route(challenge, self.solvers, automation_enabled=master_enabled,
                     type_enabled=enabled_by_type.get(challenge.captcha_type, True))
        # OCR and other unapproved solvers propose in shadow: measured, never submitted.
        for solver in plan.shadow:
            await self._shadow_attempt(solver, challenge)
        for solver in plan.automated:
            telemetry_bus.record(
                level="INFO",
                subsystem="engine:captcha",
                message=f"[CAPTCHA_SOLVER_ATTEMPT] Attempting challenge {challenge.id} with solver '{solver.name}'",
                context={"challenge_id": challenge.id, "solver": solver.solver_id},
                tier="engine",
            )
            logger.info("Attempting CAPTCHA [%s] with solver [%s]", challenge.id, solver.name)
            challenge_lifecycle.advance(challenge, challenge_lifecycle.SOLVING, f"trying {solver.solver_id}")
            challenge.status = CaptchaStatus.SOLVING
            if solver.solver_id == "automated_browser":
                challenge.params = {
                    **(challenge.params or {}),
                    "coordinator_state": "clearcote_active",
                    "solver": solver.solver_id,
                }
                self._persist_challenge(challenge)
            try:
                solution = await solver.solve(challenge)
                if solution is None:
                    telemetry_bus.record(level="INFO", subsystem="engine:captcha",
                                         message=f"[CAPTCHA_SOLVER_NO_ANSWER] '{solver.name}' returned nothing for {challenge.id}",
                                         context={"challenge_id": challenge.id, "solver": solver.solver_id}, tier="engine")
                    continue
                if self.solve_challenge(challenge.id, solution, solver_id=solver.solver_id):
                    telemetry_bus.record(
                        level="INFO",
                        subsystem="engine:captcha",
                        message=f"[CAPTCHA_ANSWERED] Challenge {challenge.id} answered by '{solver.name}'; awaiting the site's verdict",
                        context={"challenge_id": challenge.id, "solver": solver.solver_id},
                        tier="engine",
                    )
                    return solution
                telemetry_bus.record(level="WARN", subsystem="engine:captcha",
                                     message=f"[CAPTCHA_SOLVER_INVALID] '{solver.name}' returned an unusable answer for {challenge.id}",
                                     context={"challenge_id": challenge.id, "solver": solver.solver_id, "error": challenge.error},
                                     tier="engine")
            except Exception as exc:
                telemetry_bus.record(
                    level="WARN",
                    subsystem="engine:captcha",
                    message=f"[CAPTCHA_SOLVER_FAILED] Solver '{solver.name}' failed on challenge {challenge.id}: {exc}",
                    context={"challenge_id": challenge.id, "solver": solver.solver_id, "error": str(exc)},
                    tier="engine",
                )
                logger.warning("Solver [%s] failed on [%s]: %s", solver.name, challenge.id, exc)

        # 2. Interactive UI solver fallback
        challenge.status = CaptchaStatus.PENDING
        # Reserve a fresh, explicit manual window.  The automatic attempt may
        # have consumed most of the original challenge lifetime.
        challenge.expires_at = time.time() + max(float(challenge.timeout_seconds or 90.0), 180.0)
        challenge.error = None
        challenge.params = {
            **(challenge.params or {}),
            "coordinator_state": "manual_required",
            "manual_reason": "Clearcote did not finish the challenge",
        }
        challenge_lifecycle.advance(challenge, challenge_lifecycle.MANUAL,
                                    "no automatic solver answered" if plan.automated else "no automatic solver can answer this")
        self._persist_challenge(challenge)
        loopback_url = self.loopback_solver.get_url(challenge.id) if self.loopback_solver.can_solve(challenge) else None
        telemetry_bus.record(
            level="WARN",
            subsystem="engine:captcha",
            message=f"[CAPTCHA_WAITING_USER] Solver cascade suspended; waiting for interactive user response (loopback: {'yes' if loopback_url else 'none'})",
            context={"challenge_id": challenge.id, "loopback": bool(loopback_url)},
            tier="engine",
        )
        if self._manual_required_callback is not None:
            try:
                self._manual_required_callback(challenge)
            except Exception as exc:
                logger.error("Manual-required callback failed: %s", exc)
        if self.interactive_ui.enabled:
            return await self.interactive_ui.solve(challenge)

        raise RuntimeError(f"No solver available to solve CAPTCHA {challenge.id}")

    async def _shadow_attempt(self, solver: CaptchaSolver, challenge: CaptchaChallenge) -> None:
        """Let an unapproved solver propose an answer that is only recorded.

        The proposal never reaches solve_challenge, a provider or the task; the
        telemetry keeps its shape (length, confidence), not the answer itself.
        """
        from .telemetry import telemetry_bus
        try:
            proposal = await solver.solve(challenge)
        except Exception as exc:
            proposal, error = None, str(exc)
        else:
            error = None
        text = str((proposal or {}).get("text") or "")
        telemetry_bus.record(
            level="INFO", subsystem="engine:captcha",
            message=f"[CAPTCHA_SHADOW] '{solver.name}' {'proposed an answer' if proposal else 'abstained'} for {challenge.id} (not submitted)",
            context={"challenge_id": challenge.id, "solver": solver.solver_id, "abstained": proposal is None,
                     "answer_length": len(text), "confidence": (proposal or {}).get("confidence"), "error": error},
            tier="engine",
        )

    def request_solution_sync(self, challenge: CaptchaChallenge) -> dict[str, Any]:
        """Synchronous wrapper for worker processes/threads."""
        sync_event = threading.Event()
        with self._lock:
            self._challenges[challenge.id] = challenge
            self._sync_events[challenge.id] = sync_event

        loop = asyncio.new_event_loop()
        result_holder: dict[str, Any] = {}
        error_holder: list[Exception] = []

        def _runner() -> None:
            asyncio.set_event_loop(loop)
            try:
                res = loop.run_until_complete(self.request_solution(challenge))
                result_holder["solution"] = res
            except Exception as ex:
                error_holder.append(ex)
            finally:
                loop.close()

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=challenge.timeout_seconds + 5)

        if error_holder:
            raise error_holder[0]
        if "solution" in result_holder:
            return result_holder["solution"]
        raise TimeoutError(f"CAPTCHA {challenge.id} timed out")

    async def enqueue_for_ui(self, challenge: CaptchaChallenge) -> dict[str, Any]:
        """Single-dialog flood control: enqueues challenge for user modal display."""
        future = self._futures.get(challenge.id)
        if not future:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._futures[challenge.id] = future

        with self._lock:
            if challenge.id not in self._ui_queue and challenge.id != self._active_ui_challenge_id:
                self._ui_queue.append(challenge.id)
            self._promote_ui_queue()

        preferences = self.store.get_setting("captcha_ui_preferences", {}) if self.store else {}
        configured_seconds = int(preferences.get("captchaAutoSkipSecs", 0) or 0)
        auto_skip_deadline = (
            time.monotonic() + float(configured_seconds)
            if bool(preferences.get("captchaAutoSkip", False)) and configured_seconds > 0
            else None
        )

        # Poll in short, shielded intervals so an explicit visible Clearcote
        # promotion can take ownership of a queue that was already waiting.
        # A single wait_for(timeout=30) cannot observe the promotion and would
        # expire the future underneath a valid late solver result.
        while True:
            if future.done():
                return await future
            with self._lock:
                visible_solver_active = bool((challenge.params or {}).get("manual_solver_active"))
                remaining = challenge.time_remaining()
            if remaining <= 0:
                reason = "Challenge expired"
            elif visible_solver_active:
                # Visible Clearcote owns the challenge until it returns; the
                # short UI auto-skip deadline no longer applies.
                reason = None
            elif auto_skip_deadline is not None and time.monotonic() >= auto_skip_deadline:
                reason = "User challenge timed out"
            else:
                reason = None
            if reason:
                with self._lock:
                    if not future.done():
                        challenge.status = CaptchaStatus.EXPIRED
                        challenge.error = reason
                        self._persist_challenge(challenge)
                        self._advance_ui_queue(challenge.id)
                raise TimeoutError(reason)

            wait_seconds = min(1.0, max(0.05, remaining))
            if not visible_solver_active and auto_skip_deadline is not None:
                wait_seconds = min(wait_seconds, max(0.05, auto_skip_deadline - time.monotonic()))
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=wait_seconds)
            except asyncio.TimeoutError:
                continue

    def _promote_ui_queue(self) -> None:
        """Promote the next challenge to active UI dialog if none currently active."""
        if self._active_ui_challenge_id is None and self._ui_queue:
            next_id = self._ui_queue.pop(0)
            self._active_ui_challenge_id = next_id
            active = self._challenges.get(next_id)
            if active:
                logger.info("Promoted CAPTCHA [%s] to active UI prompt", next_id)
                loopback = self.loopback_solver.get_url(active.id) if self.loopback_solver.can_solve(active) else None
                self._emit_event("CaptchaCreated", active.task_id, {
                    "challenge": active.to_dict(),
                    "loopback_url": loopback,
                })
                try:
                    from .telemetry import telemetry_bus
                    telemetry_bus.record(
                        level="WARN",
                        subsystem="engine:captcha",
                        message=(
                            f"[CAPTCHA_PROMPT] Ready for solving: {active.captcha_type} for {active.provider_id}. "
                            + (f"Browser loopback URL: {loopback}" if loopback else "Solve in Captchas tab")
                        ),
                        context={
                            "task_id": active.task_id,
                            "challenge_id": active.id,
                            "loopback_url": loopback,
                            "provider": active.provider_id,
                            "captcha_type": str(active.captcha_type),
                        },
                        tier="engine",
                    )
                except Exception:
                    pass

    def _advance_ui_queue(self, completed_id: str) -> None:
        """Release current dialog and promote next in queue."""
        if self._active_ui_challenge_id == completed_id:
            self._active_ui_challenge_id = None
        if completed_id in self._ui_queue:
            self._ui_queue.remove(completed_id)
        self._promote_ui_queue()

    def solve_challenge(self, challenge_id: str, solution: dict[str, Any] | str, solver_id: str = "interactive_ui") -> bool:
        """Marks challenge solved, fulfills waiting future/threads, and emits event."""
        with self._lock:
            challenge = self.get_challenge(challenge_id)
            if not challenge or challenge.status in (CaptchaStatus.SOLVED, CaptchaStatus.EXPIRED,
                                                     CaptchaStatus.SKIPPED, CaptchaStatus.VERIFYING):
                return False

            solution_value: dict[str, Any] = solution if isinstance(solution, dict) else {"token": solution}
            token_types = {
                CaptchaType.RECAPTCHA_V2, CaptchaType.RECAPTCHA_V3,
                CaptchaType.HCAPTCHA, CaptchaType.TURNSTILE,
            }
            if challenge.captcha_type in token_types and not solution_token(solution_value):
                challenge.error = "CAPTCHA solver returned no token"
                self._persist_challenge(challenge)
                return False
            if not solution_value:
                challenge.error = "CAPTCHA solver returned an empty solution"
                self._persist_challenge(challenge)
                return False

            if not challenge_lifecycle.advance(challenge, challenge_lifecycle.VERIFYING, f"answer received from {solver_id}"):
                return False
            # Received is not solved: the site's verdict settles it (challenge_verifier).
            challenge.status = CaptchaStatus.VERIFYING
            # Keep the raw value only in the in-memory challenge/future.  Its
            # serialized representation is redacted by to_dict().
            challenge.solution = solution_value
            challenge.solver_used = solver_id
            challenge.resolved_at = time.time()
            self._persist_challenge(challenge)

            fut = self._futures.get(challenge_id)
            self._complete_future(fut, value=solution_value)

            sync_ev = self._sync_events.get(challenge_id)
            if sync_ev:
                sync_ev.set()

            self._advance_ui_queue(challenge_id)

            self._emit_event("CaptchaResolved", challenge.task_id, {
                "challenge_id": challenge.id,
                "solution_received": True,
                "token_received": bool(solution_token(solution_value)),
                "solver_id": solver_id,
            })
            from .telemetry import telemetry_bus
            telemetry_bus.record(
                level="INFO",
                subsystem="engine:captcha",
                message=f"[CAPTCHA_SOLVED_SUCCESS] Challenge {challenge.id} ({challenge.captcha_type}) solved by solver '{solver_id}'",
                context={"challenge_id": challenge.id, "solver": solver_id, "has_token": bool(solution_token(solution_value))},
                tier="engine",
            )
            return True

    def skip_challenge(self, challenge_id: str, scope: str = "single", duration_seconds: float = 300.0) -> bool:
        """Skips a challenge or suppresses all challenges for host."""
        with self._lock:
            challenge = self.get_challenge(challenge_id)
            if not challenge:
                return False

            challenge_lifecycle.advance(challenge, challenge_lifecycle.SKIPPED, f"skipped by user ({scope})")
            challenge.status = CaptchaStatus.SKIPPED
            challenge.error = f"Skipped by user ({scope})"
            challenge.resolved_at = time.time()

            if scope == "host":
                self._host_skips[challenge.provider_id] = time.time() + duration_seconds
                logger.info("Host [%s] CAPTCHAs skipped for %.1f seconds", challenge.provider_id, duration_seconds)

            self._persist_challenge(challenge)

            fut = self._futures.get(challenge_id)
            self._complete_future(fut, exc=RuntimeError(challenge.error))

            sync_ev = self._sync_events.get(challenge_id)
            if sync_ev:
                sync_ev.set()

            self._advance_ui_queue(challenge_id)

            self._emit_event("CaptchaSkipped", challenge.task_id, {
                "challenge_id": challenge.id,
                "scope": scope,
            })
            return True

    def cancel_challenges_for_task(self, task_id: str) -> None:
        """Cancel and dismiss all pending or active challenges for a canceled/deleted task."""
        with self._lock:
            for cid, ch in list(self._challenges.items()):
                if ch.task_id == task_id and ch.status in (CaptchaStatus.PENDING, CaptchaStatus.SOLVING, CaptchaStatus.VERIFYING):
                    challenge_lifecycle.advance(ch, challenge_lifecycle.CANCELLED, "task canceled or deleted")
                    ch.status = CaptchaStatus.SKIPPED
                    ch.error = "Task canceled or deleted"
                    ch.resolved_at = time.time()
                    self._persist_challenge(ch)
                    fut = self._futures.get(cid)
                    self._complete_future(fut, exc=asyncio.CancelledError("Task canceled or deleted"))
                    sync_ev = self._sync_events.get(cid)
                    if sync_ev:
                        sync_ev.set()
                    self._advance_ui_queue(cid)
                    self._emit_event("CaptchaSkipped", task_id, {"challenge_id": cid, "scope": "task_cancel"})

    def report_result(self, challenge_id: str, valid: bool) -> None:
        """Propagate verification outcome to solvers (e.g. 2Captcha refund or local calibration)."""
        with self._lock:
            challenge = self.get_challenge(challenge_id)
            if not challenge or not challenge.solver_used:
                return

            for solver in self.solvers:
                if solver.solver_id == challenge.solver_used:
                    solver.report_result(challenge, valid)
                    break

    def get_config(self) -> dict[str, Any]:
        """Return solver preferences and configuration."""
        with self._lock:
            solvers_config = []
            for s in self.solvers:
                info: dict[str, Any] = {
                    "id": s.solver_id,
                    "name": s.name,
                    "priority": s.priority,
                    "enabled": s.enabled,
                }
                if isinstance(s, ThirdPartyApiSolver):
                    info["has_key"] = bool(s.api_key)
                    info["masked_key"] = (s.api_key[:4] + "..." + s.api_key[-4:]) if len(s.api_key) > 8 else ("***" if s.api_key else "")
                elif s.solver_id == "audio_speech":
                    svc = getattr(s, "speech_service", "wit")
                    active_key = getattr(s, "wit_api_key", "") if svc == "wit" else getattr(s, "google_api_key", "")
                    info["speech_service"] = svc
                    info["has_key"] = bool(active_key)
                    info["masked_key"] = (active_key[:4] + "..." + active_key[-4:]) if active_key and len(active_key) > 8 else ("***" if active_key else "")
                solvers_config.append(info)

            sound_enabled = True
            if self.store and hasattr(self.store, "get_setting"):
                sound_enabled = self.store.get_setting("captcha_sound_enabled", True)

            auto_skip = True
            if self.store and hasattr(self.store, "get_setting"):
                auto_skip = bool(self.store.get_setting("captcha_auto_skip", True))
            return {
                "solvers": solvers_config,
                "sound_enabled": sound_enabled,
                "auto_skip_timeout": auto_skip,
                "auto_open_browser": getattr(self.loopback_solver, "auto_open", True),
            }

    def set_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Update solver preferences and API keys."""
        with self._lock:
            if "solvers" in config:
                for sc in config["solvers"]:
                    s_id = sc.get("id")
                    for s in self.solvers:
                        if s.solver_id == s_id:
                            if "enabled" in sc:
                                s.enabled = bool(sc["enabled"])
                            if "priority" in sc:
                                s.priority = int(sc["priority"])
                            if isinstance(s, ThirdPartyApiSolver) and "api_key" in sc:
                                key = sc["api_key"].strip()
                                if key:
                                    s.api_key = key
                                    s.enabled = True
                            elif s.solver_id == "audio_speech":
                                if "speech_service" in sc:
                                    s.speech_service = sc["speech_service"]
                                if "api_key" in sc and sc["api_key"].strip():
                                    key = sc["api_key"].strip()
                                    if getattr(s, "speech_service", "wit") == "wit":
                                        s.wit_api_key = key
                                    else:
                                        s.google_api_key = key

            if "auto_open_browser" in config:
                self.loopback_solver.auto_open = bool(config["auto_open_browser"])
            if "sound_enabled" in config and self.store and hasattr(self.store, "set_setting"):
                self.store.set_setting("captcha_sound_enabled", bool(config["sound_enabled"]))
            if "auto_skip_timeout" in config and self.store and hasattr(self.store, "set_setting"):
                self.store.set_setting("captcha_auto_skip", bool(config["auto_skip_timeout"]))

            self._sort_solvers()
            if self.store and hasattr(self.store, "set_setting"):
                self.store.set_setting("captcha_config", self.get_config())

            return self.get_config()

    def _persist_challenge(self, challenge: CaptchaChallenge) -> None:
        if self.store and hasattr(self.store, "save_captcha_challenge"):
            try:
                self.store.save_captcha_challenge(challenge)
            except Exception as exc:
                logger.error("Failed saving captcha challenge: %s", exc)
        coordinator_state = (challenge.params or {}).get("coordinator_state")
        if coordinator_state:
            self._emit_event("CaptchaStateChanged", challenge.task_id, {
                "challenge_id": challenge.id,
                "group_id": (challenge.params or {}).get("group_id"),
                "origin_host": (challenge.params or {}).get("origin_host"),
                "coordinator_state": coordinator_state,
                "solver": (challenge.params or {}).get("solver"),
                "challenge": challenge.to_dict(),
            })
            if self._coordinator_callback is not None:
                try:
                    self._coordinator_callback(challenge.task_id, coordinator_state, dict(challenge.params or {}))
                except Exception as exc:
                    logger.error("Coordinator stage callback failed: %s", exc)

    def _emit_event(self, event_type: str, task_id: str | None, payload: dict[str, Any]) -> None:
        if self.events and hasattr(self.events, "emit"):
            try:
                self.events.emit(event_type, task_id, payload)
            except Exception as exc:
                logger.error("Failed emitting captcha event: %s", exc)

    def _cleanup_expired(self) -> None:
        now = time.time()
        for c in list(self._challenges.values()):
            if c.status in {CaptchaStatus.PENDING, CaptchaStatus.SOLVING} and c.expires_at <= now:
                challenge_lifecycle.advance(c, challenge_lifecycle.EXPIRED, "no answer before the deadline")
                c.status = CaptchaStatus.EXPIRED
                c.error = "Challenge expired"
                self._persist_challenge(c)
                self._advance_ui_queue(c.id)
                self._complete_future(self._futures.get(c.id), exc=TimeoutError("Challenge expired"))
