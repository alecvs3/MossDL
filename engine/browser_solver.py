"""High-speed automated multi-tab browser solver daemon (Clearcote + Camoufox/Patchright fallback).

Features:
- Multi-Tab Parallel Pre-Resolution: Coordinates up to max_lanes (default 3) concurrent tabs, each in its own browser context
- Staggered Launch: 1.2s minimum interval between concurrent tab creations to prevent WAF bot triggers
- Primary Tier: Clearcote Chromium via async Patchright
- Secondary Tier: Async Camoufox (Gecko engine) or System Chrome fallback
- On-Demand Warm Context: Stays warm during active queue processing, auto-reaped to 0 MB RAM after 60s idle
- Ad & Media Interception: Native route blocking for ad networks and useless media
- Clearance Harvesting: Automatic injection of cf_clearance cookies into engine.http_client
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from .clearcote_manager import get_clearcote_executable, get_clearcote_status
from .dom_cleaner import is_ad_domain, is_solver_blocked_embed
from . import route_http
from .http_client import clearance_cache
from .telemetry import telemetry_bus
from .timer_detector import TimerDetector
from . import critical_trace
from .browser_step import advance_and_wait, step2_click_ready

logger = logging.getLogger(__name__)


def _url_filename(url: str) -> str:
    """The decoded basename of a URL path, ignoring any query string."""
    if not url:
        return ""
    path = urllib.parse.urlsplit(url).path
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    return name.strip().lower()


def direct_url_matches_request(requested_url: str, direct_url: str) -> bool:
    """Reject a captured link that belongs to a different file than was asked for.

    A solve that reads another lane's page yields a perfectly valid direct link
    -- for the wrong part. Handing it back writes one part's bytes into another
    part's file, which no later size or checksum check can catch, because the
    bytes really are a complete, correct volume. Only the name disagrees.

    Links whose request side carries no filename (shortlinks, id-only hoster
    pages) cannot be compared and are allowed through.
    """
    wanted = _url_filename(requested_url)
    if not wanted or "." not in wanted:
        return True
    got = _url_filename(direct_url)
    if not got or "." not in got:
        return True
    return wanted == got


IDLE_REAP_SECONDS = 60.0
DEFAULT_MAX_LANES = 3
DEFAULT_STAGGER_INTERVAL = 1.2
DATANODES_STEP_ADVANCE_ACK_SECONDS = 2.0


# Playwright resource types in the filter lists' vocabulary.
_REQUEST_TYPES = {"xhr": "xmlhttprequest", "fetch": "xmlhttprequest", "document": "sub_frame", "eventsource": "other",
                  "manifest": "other", "texttrack": "other"}

_ad_blocker_provider: Optional[Callable[[], Any]] = None


def set_ad_blocker(provider: Callable[[], Any]) -> None:
    """The engine's AdBlocker (or None while blocking is off), asked per solve."""
    global _ad_blocker_provider
    _ad_blocker_provider = provider


def _ad_blocker() -> Any:
    return _ad_blocker_provider() if _ad_blocker_provider else None


def list_verdicts(page: Any, blocker: Any, start_url: str) -> Callable[[Any], Any]:
    """An async check of one request against the filter lists, cached per URL.

    The page's own top-level navigations are never blocked: the user chose them.
    """
    verdicts: dict[str, bool] = {}

    async def blocked(request: Any) -> bool:
        if blocker is None or request.is_navigation_request() and request.frame == page.main_frame:
            return False
        if request.url not in verdicts:
            kind = _REQUEST_TYPES.get(request.resource_type, request.resource_type)
            result = await asyncio.to_thread(blocker.check, [{"url": request.url, "source_url": page.url or start_url, "type": kind}])
            verdicts[request.url] = bool(result and result[0])
        return verdicts[request.url]

    return blocked


_PAGE_CLASSES_AND_IDS = """() => {
  const classes = new Set(), ids = new Set();
  for (const el of document.querySelectorAll('[class],[id]')) {
    el.classList.forEach((c) => classes.add(c));
    if (el.id) ids.add(el.id);
  }
  return [[...classes].slice(0, 4000), [...ids].slice(0, 4000)];
}"""


async def hide_ads(page: Any, blocker: Any) -> int:
    """Hide the elements the filter lists' cosmetic rules match on this page.

    One rule per selector: a selector the browser does not support would
    otherwise void every selector grouped with it.
    """
    classes, ids = await page.evaluate(_PAGE_CLASSES_AND_IDS)
    rules = await asyncio.to_thread(blocker.cosmetic, page.url, classes, ids)
    selectors = (rules or {}).get("hide_selectors") or []
    if selectors:
        await page.add_style_tag(content="\n".join(f"{sel}{{display:none!important}}" for sel in selectors[:5000]))
    return len(selectors)


class BrowserSolverDaemon:
    """Daemon managing on-demand warm browser sessions with multi-tab concurrency on an actor thread."""

    def __init__(self, max_lanes: int = DEFAULT_MAX_LANES) -> None:
        self.max_lanes = max(1, int(max_lanes))
        self._actor_thread: Optional[threading.Thread] = None
        self._actor_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()
        self._stop_event: Optional[asyncio.Event] = None
        self._pw: Any = None
        self._browser: Any = None
        self._active_engine: Optional[str] = None
        self._last_used: float = 0.0
        self._last_tab_launch: float = 0.0
        self._stagger_lock: Optional[asyncio.Lock] = None
        self._lane_semaphore: Optional[asyncio.Semaphore] = None
        self._active_solves = 0
        self._package_solve_demand: dict[str, int] = {}
        from .solver_lane_policy import SolverLanePolicy
        self._lane_policy = SolverLanePolicy()
        self._stage_callback: Optional[Callable[[str, str, dict[str, Any]], None]] = None

    def set_stage_callback(self, cb: Optional[Callable[[str, str, dict[str, Any]], None]]) -> None:
        """Register a callback to receive real-time solver stage updates: (task_id, stage, data)."""
        self._stage_callback = cb

    def _notify_stage(self, task_id: Optional[str], stage: str, data: dict[str, Any]) -> None:
        """Notify registered listener of solver stage transitions."""
        if not task_id or not self._stage_callback:
            return
        try:
            self._stage_callback(task_id, stage, data)
        except Exception as err:
            logger.warning("solver stage callback failed for %s stage %s: %s", task_id, stage, err)

    def _ensure_actor_started(self) -> None:
        with self._actor_lock:
            if self._actor_thread is None or not self._actor_thread.is_alive():
                self._loop_ready.clear()
                self._actor_thread = threading.Thread(target=self._actor_loop, name="BrowserSolverActor", daemon=True)
                self._actor_thread.start()
                if not self._loop_ready.wait(timeout=10.0):
                    raise RuntimeError("BrowserSolver actor loop failed to start within 10s")

    def _actor_loop(self) -> None:
        """Dedicated thread running asyncio loop owning async Playwright, browser, and contexts."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._stagger_lock = asyncio.Lock()
        self._lane_semaphore = asyncio.Semaphore(self.max_lanes)
        self._stop_event = asyncio.Event()
        self._loop_ready.set()
        try:
            loop.run_until_complete(self._idle_reaper_task())
        except Exception as exc:
            logger.error("BrowserSolverActor loop terminated unexpectedly: %s", exc)
        finally:
            try:
                loop.run_until_complete(self._async_reap_browser())
            except Exception:
                pass
            loop.close()
            self._loop = None

    async def _idle_reaper_task(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=10.0)
                break
            except asyncio.TimeoutError:
                pass
            if self._active_solves == 0 and self._browser:
                if (time.time() - self._last_used) >= IDLE_REAP_SECONDS:
                    await self._async_reap_browser()

    async def _async_reap_browser(self) -> None:
        if not self._browser:
            return
        engine = self._active_engine
        try:
            if self._browser:
                # Per-solve contexts close with their solve; closing the browser
                # disposes of any that outlived an error path.
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None
        self._active_engine = None
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:captcha",
            message=f"[SOLVER_REAPED] {engine} shut down on actor thread (0 MB RAM)",
            tier="engine",
        )

    async def _wait_stagger(self, min_interval: float = DEFAULT_STAGGER_INTERVAL) -> None:
        """Enforces spacing between concurrent tab launches to prevent WAF bot triggers."""
        if not self._stagger_lock:
            return
        async with self._stagger_lock:
            now = time.monotonic()
            elapsed = now - self._last_tab_launch
            if elapsed < min_interval:
                delay = min_interval - elapsed
                critical_trace.mark(
                    "solver.stagger", event="state", resource="solver_launch",
                    wait_seconds=delay,
                )
                await asyncio.sleep(delay)
            self._last_tab_launch = time.monotonic()

    async def _ensure_browser(self) -> tuple[Any, str]:
        """Return the shared browser; each solve opens its own context.

        Contexts are deliberately NOT shared. Hosters like DataNodes register a
        pending download server-side against the session cookie and then serve it
        from one shared URL (``/download``), so concurrent solves in a single
        cookie jar overwrite each other: the last step-one POST wins and the
        other lanes read a sibling's page. Turnstile still passes -- clearance is
        domain-wide -- so the solve reports success while the per-file link is
        silently lost, and the task re-resolves forever.
        """
        self._last_used = time.time()
        if self._browser:
            return self._browser, self._active_engine or "unknown"

        from patchright.async_api import async_playwright

        if not self._pw:
            self._pw = await async_playwright().start()

        # 1. Try Clearcote (Primary)
        clearcote_exe = get_clearcote_executable()
        if clearcote_exe and clearcote_exe.exists():
            try:
                t0 = time.perf_counter()
                browser = await self._pw.chromium.launch(
                    executable_path=str(clearcote_exe),
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
                self._browser = browser
                self._active_engine = "clearcote"
                dur = (time.perf_counter() - t0) * 1000.0
                telemetry_bus.record(
                    level="INFO",
                    subsystem="engine:captcha",
                    message=f"[SOLVER_LAUNCHED] Clearcote Chromium active in {dur:.1f}ms",
                    context={"engine": "clearcote", "duration_ms": dur},
                    tier="engine",
                )
                return self._browser, "clearcote"
            except Exception as exc:
                telemetry_bus.record(
                    level="WARN",
                    subsystem="engine:captcha",
                    message=f"[SOLVER_FALLBACK] Clearcote launch failed ({exc}); falling back to secondary",
                    context={"error": str(exc)},
                    tier="engine",
                )

        # 2. Try Camoufox (Secondary Gecko Fallback)
        try:
            from camoufox.async_api import AsyncCamoufox
            t0 = time.perf_counter()
            cm = AsyncCamoufox(headless=True, block_images=True, block_webrtc=True)
            browser = await cm.__aenter__()
            self._browser = browser
            self._active_engine = "camoufox"
            dur = (time.perf_counter() - t0) * 1000.0
            telemetry_bus.record(
                level="INFO",
                subsystem="engine:captcha",
                message=f"[SOLVER_LAUNCHED] Camoufox Gecko active in {dur:.1f}ms (fallback)",
                context={"engine": "camoufox", "duration_ms": dur},
                tier="engine",
            )
            return self._browser, "camoufox"
        except Exception as exc_camou:
            logger.debug("Camoufox fallback not available: %s", exc_camou)

        # 3. Try System Google Chrome via Patchright
        chrome_candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        chrome_failures: list[str] = []
        for chrome_path in chrome_candidates:
            if os.path.exists(chrome_path):
                try:
                    t0 = time.perf_counter()
                    browser = await self._pw.chromium.launch(executable_path=chrome_path, headless=True)
                    self._browser = browser
                    self._active_engine = "system_chrome"
                    dur = (time.perf_counter() - t0) * 1000.0
                    telemetry_bus.record(
                        level="INFO",
                        subsystem="engine:captcha",
                        message=f"[SOLVER_LAUNCHED] System Chrome active in {dur:.1f}ms (fallback)",
                        context={"engine": "system_chrome", "duration_ms": dur},
                        tier="engine",
                    )
                    return self._browser, "system_chrome"
                except Exception as err:
                    chrome_failures.append(f"{Path(chrome_path).name}: {err}")
                    logger.warning("System Chrome launch failed for %s: %s", chrome_path, err)

        detail = f"; chrome candidates: {' | '.join(chrome_failures)}" if chrome_failures else ""
        raise RuntimeError(
            f"No automated browser solver available (Clearcote, Camoufox, or Chrome required){detail}")

    async def _async_execute_solve(
        self,
        url: str,
        timeout_seconds: float = 90.0,
        initial_cookies: Optional[dict[str, str]] = None,
        task_id: Optional[str] = None,
        completion_mode: str = "direct_or_token_wait",
    ) -> dict[str, Any]:
        """Navigate to URL, bypass Turnstile/Cloudflare, extract clearance cookies and direct links."""
        browser, engine_name = await self._ensure_browser()
        t0 = time.perf_counter()
        pfx = f"[{task_id[:8]}] " if task_id else ""
        t_ctx = {"task_id": task_id} if task_id else {}

        if not url:
            telemetry_bus.record(
                level="ERROR",
                subsystem="engine:captcha",
                message=f"{pfx}[SOLVER_INVALID_URL] Cannot execute solve: url is empty or None",
                context={**t_ctx, "url": str(url), "engine": engine_name},
                tier="engine",
            )
            return {"success": False, "engine": engine_name, "error": "Invalid or empty URL provided for solving"}

        # A private cookie jar per solve: the hoster's pending-download state is
        # keyed to the session, so sharing one would let concurrent lanes clobber
        # each other's link. Cached clearance is re-injected below, which is what
        # keeps a package at one user click despite the isolation.
        # The solve's route, per context: one shared browser serves every route.
        ctx = await browser.new_context(viewport={"width": 1280, "height": 720},
                                        proxy=route_http.playwright_proxy())

        if initial_cookies:
            try:
                host = urllib.parse.urlsplit(url).hostname or ""
                cookie_list = [{"name": str(k), "value": str(v), "domain": host, "path": "/"} for k, v in initial_cookies.items()]
                await ctx.add_cookies(cookie_list)
            except Exception:
                pass

        target_host = (urllib.parse.urlsplit(url).hostname or "").lower()
        allowed_nav_hosts = {target_host, "challenges.cloudflare.com", "cloudflare.com", "dlproxy.uk", "mediafire.com"}

        await self._wait_stagger(DEFAULT_STAGGER_INTERVAL)
        page: Any = None
        try:
            page = await ctx.new_page()
            step_request_event = asyncio.Event()

            def _on_request(request: Any) -> None:
                try:
                    request_host = (urllib.parse.urlsplit(request.url).hostname or "").lower()
                    if request_host == target_host and str(request.method).upper() == "POST":
                        step_request_event.set()
                        critical_trace.mark(
                            "solver.step_advance.request", event="state",
                            method="POST", host=request_host,
                        )
                except Exception:
                    pass

            blocker = _ad_blocker()
            blocked_by_lists = list_verdicts(page, blocker, url)

            async def route_handler(route: Any) -> None:
                r = route.request
                r_url = r.url.lower()
                if r.is_navigation_request():
                    r_host = (urllib.parse.urlsplit(r_url).hostname or "").lower()
                    if not any(a in r_host for a in allowed_nav_hosts):
                        await route.abort()
                        return
                if "cloudflare" in r_url:
                    await route.continue_()
                elif is_ad_domain(r_url) or is_solver_blocked_embed(r_url) or any(ad in r_url for ad in ("dearthsongman", "monster", "cfd", "qpon", "popads", "adsterra", "monetag")):
                    await route.abort()
                elif await blocked_by_lists(r):
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()

            await page.route("**/*", route_handler)
            page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
            page.on("popup", lambda p: asyncio.create_task(p.close()))
            page.on("request", _on_request)

            telemetry_bus.record(
                level="INFO",
                subsystem="engine:captcha",
                message=f"{pfx}[CHALLENGE_NAVIGATE] Navigating to {url} with {engine_name}...",
                context={**t_ctx, "url": url, "engine": engine_name},
                tier="engine",
            )
            self._notify_stage(task_id, "navigating", {"url": url})

            captured_direct_url: Optional[str] = None

            def _is_valid_direct_link(cand: str) -> bool:
                if not cand or not isinstance(cand, str) or not cand.startswith("http"):
                    return False
                cand_clean = cand.strip()
                cand_split = urllib.parse.urlsplit(cand_clean)
                cand_path = cand_split.path.rstrip("/")
                if cand_clean == url.strip() or cand_path in ("", "/download", "/f", "/file"):
                    return False
                cand_host = (cand_split.hostname or "").lower()
                if is_ad_domain(cand_clean) or any(ad in cand_host for ad in ("shop", "monster", "cyou", "top", "buzz", "click")):
                    return False
                return True

            async def _on_response(response: Any) -> None:
                nonlocal captured_direct_url
                if captured_direct_url:
                    return
                try:
                    resp_url_l = response.url.lower()
                    if is_ad_domain(resp_url_l):
                        return
                    ct = (response.headers.get("content-type") or "").lower()
                    if response.status == 200 and ("application/json" in ct or "text/json" in ct or "text/plain" in ct or "/download" in resp_url_l):
                        host_target = (urllib.parse.urlsplit(url).hostname or "").lower()
                        if not any(trusted in resp_url_l for trusted in (host_target, "datanodes.to", "dlproxy.uk", "mediafire.com")):
                            return
                        body = None
                        try:
                            body = await response.json()
                        except Exception:
                            body = None
                        if isinstance(body, dict):
                            link = body.get("link") or body.get("direct-link") or body.get("url") or body.get("direct")
                            if link and isinstance(link, str):
                                link = urllib.parse.unquote(link)
                                if _is_valid_direct_link(link):
                                    captured_direct_url = link
                                    self._notify_stage(task_id, "primed", {"direct_url": link})
                                    telemetry_bus.record(
                                        level="INFO",
                                        subsystem="engine:captcha",
                                        message=f"{pfx}[SOLVER_INTERCEPT] Captured direct URL via response interception",
                                        context={**t_ctx, "direct_url": link[:120], "engine": engine_name},
                                        tier="engine",
                                    )
                    elif response.status in (301, 302, 303, 307, 308):
                        loc = response.headers.get("location") or ""
                        if _is_valid_direct_link(loc):
                            loc_host = (urllib.parse.urlsplit(loc).hostname or "").lower()
                            if any(trusted in loc_host for trusted in ("dlproxy.uk", "mediafire.com")):
                                captured_direct_url = loc
                                self._notify_stage(task_id, "primed", {"direct_url": loc})
                except Exception:
                    pass

            async def _on_download(download: Any) -> None:
                nonlocal captured_direct_url
                try:
                    d_url = download.url
                    if d_url and _is_valid_direct_link(d_url):
                        captured_direct_url = d_url
                        self._notify_stage(task_id, "primed", {"direct_url": d_url})
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.create_task(_on_response(r)))
            page.on("download", lambda d: asyncio.create_task(_on_download(d)))

            try:
                with critical_trace.span(
                    "solver.navigation", resource="browser", resource_id=engine_name,
                    host=target_host,
                ):
                    await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_seconds * 1000))
                if blocker is not None:
                    await hide_ads(page, blocker)
            except Exception as exc:
                telemetry_bus.record(
                    level="ERROR",
                    subsystem="engine:captcha",
                    message=f"{pfx}[SOLVER_NAVIGATION_FAILED] Browser navigation failed: {exc}",
                    context={**t_ctx, "url": url, "engine": engine_name, "page_type": type(page).__name__},
                    tier="engine",
                )
                return {"success": False, "engine": engine_name, "error": f"Browser navigation failed: {exc}"}

            # Step advance: If page has an advance button (e.g. #method_free on DataNodes), click it
            saw_step_advance = False
            clicked_step2_button = False
            try:
                btn_free = await page.query_selector("#method_free")
                if btn_free:
                    saw_step_advance = True
                    self._notify_stage(task_id, "step_advance", {})
                    try:
                        await page.evaluate("() => { const b = document.getElementById('method_free'); if (b) b.disabled = false; }")
                    except Exception:
                        pass
                    telemetry_bus.record(
                        level="INFO",
                        subsystem="engine:captcha",
                        message=f"{pfx}[SOLVER_STEP_ADVANCE] Clicking #method_free to advance to download step...",
                        context={**t_ctx, "url": url, "engine": engine_name},
                        tier="engine",
                    )
                    with critical_trace.span(
                        "solver.step_advance", resource="browser", resource_id=engine_name,
                        acknowledgement="readiness_union",
                    ):
                        step_request_event.clear()
                        await advance_and_wait(
                            page,
                            btn_free,
                            step_selector="#method_free",
                            readiness_selectors=(
                                "download-countdown",
                                ".cf-turnstile",
                                "[data-sitekey]",
                                "[name='cf-turnstile-response']",
                                "#download-link",
                            ),
                            request_event=step_request_event,
                            click_script="() => document.getElementById('method_free')?.click()",
                            # Live traces show DataNodes' first synthetic click
                            # occasionally dispatches nothing. Waiting 12 seconds
                            # before the evidence-gated retry dominated the whole
                            # solve; two seconds is ample to observe a POST, DOM,
                            # URL, timer, widget, or response on this host.
                            timeout_seconds=(
                                DATANODES_STEP_ADVANCE_ACK_SECONDS
                                if target_host == "datanodes.to"
                                else 12.0
                            ),
                        )
            except Exception:
                pass

            t_poll_start = time.perf_counter()
            resolved = False
            final_title = ""
            harvested_cookies: dict[str, str] = {}
            turnstile_token: Optional[str] = None
            token_acquired_at: Optional[float] = None
            saw_turnstile = False
            saw_cloudflare_interstitial = False
            last_heartbeat = t_poll_start
            timer_detected = False
            timer_start_time: Optional[float] = None
            timer_initial_seconds: int = 0
            timer_source: str = "none"
            last_notified_sec: Optional[int] = None
            timer_baseline_url = ""
            timer_baseline_title = ""
            timer_parse_miss_url = ""

            while (time.perf_counter() - t_poll_start) < timeout_seconds:
                elapsed = time.perf_counter() - t_poll_start
                try:
                    cookies_list = await ctx.cookies()
                    cookies = {c["name"]: c["value"] for c in cookies_list}
                except Exception:
                    cookies = {}
                try:
                    title = await page.title() or ""
                except Exception:
                    title = ""
                final_title = title
                try:
                    current_url = page.url or url
                except Exception:
                    current_url = url

                # Re-baseline countdown tracking whenever the page navigates or the
                # document title changes: a new page means a new (or absent) countdown.
                title_changed = bool(title) and bool(timer_baseline_title) and title != timer_baseline_title
                if current_url != timer_baseline_url or title_changed:
                    if timer_detected or last_notified_sec is not None:
                        telemetry_bus.record(
                            level="INFO",
                            subsystem="engine:captcha",
                            message=f"{pfx}[TIMER_REBASELINE] Page changed ({current_url!r}, title={title!r}) — resetting countdown tracking",
                            context={
                                **t_ctx,
                                "url": current_url,
                                "title": title,
                                "previous_title": timer_baseline_title,
                                "timer_source": timer_source,
                                "previous_seconds": timer_initial_seconds,
                                "previous_remaining": last_notified_sec,
                            },
                            tier="engine",
                        )
                    timer_baseline_url = current_url
                    timer_baseline_title = title
                    timer_detected = False
                    timer_start_time = None
                    timer_initial_seconds = 0
                    timer_source = "none"
                    last_notified_sec = None

                if time.perf_counter() - last_heartbeat >= 3.0:
                    last_heartbeat = time.perf_counter()
                    telemetry_bus.record(
                        level="DEBUG",
                        subsystem="engine:captcha",
                        message=f"{pfx}[SOLVER_HEARTBEAT] Solving in progress ({elapsed:.1f}s elapsed, engine={engine_name}, saw_turnstile={saw_turnstile})",
                        context={**t_ctx, "elapsed_s": round(elapsed, 1), "engine": engine_name, "saw_turnstile": saw_turnstile,
                                 "has_token": bool(turnstile_token), "has_direct_url": bool(captured_direct_url)},
                        tier="engine",
                    )

                if captured_direct_url:
                    resolved = True
                    harvested_cookies = cookies
                    break

                # Timer detection and second-by-second countdown tracking (runs in parallel with Turnstile solve)
                try:
                    dom_timer = await page.evaluate(TimerDetector.extract_dom_script())
                    live_html = ""
                    if not (dom_timer and dom_timer.get("has_timer")):
                        # Fallback: parse the live DOM with the server-side ranked detector,
                        # which is robust to Vue attribute/prop variations the JS snippet
                        # can miss (e.g. <download-countdown :countdown="10">).
                        try:
                            live_html = await page.content()
                            fallback_candidates = TimerDetector.detect_candidates(live_html)
                            fallback_best = TimerDetector.best_candidate(fallback_candidates)
                            if fallback_best is not None:
                                dom_timer = {
                                    "has_timer": True,
                                    "seconds": fallback_best.seconds,
                                    "source": f"html:{fallback_best.source}",
                                }
                        except Exception as fallback_exc:
                            telemetry_bus.record(
                                level="DEBUG",
                                subsystem="engine:captcha",
                                message=f"{pfx}[TIMER_HTML_FALLBACK_FAILED] {fallback_exc}",
                                context={**t_ctx, "url": url},
                                tier="engine",
                            )
                    if not (dom_timer and dom_timer.get("has_timer")):
                        # Loud parse-miss diagnostic (once per page) with a DOM snippet.
                        if current_url != timer_parse_miss_url:
                            timer_parse_miss_url = current_url
                            snippet = re.sub(r"\s+", " ", live_html or "")[:240]
                            telemetry_bus.record(
                                level="WARN",
                                subsystem="engine:captcha",
                                message=f"{pfx}[TIMER_PARSE_MISS] No countdown timer parsed on page (url={current_url!r}, title={title!r})",
                                context={**t_ctx, "url": current_url, "title": title, "dom_snippet": snippet},
                                tier="engine",
                            )
                    if dom_timer and dom_timer.get("has_timer"):
                        curr_secs = int(dom_timer.get("seconds", 0))
                        if not timer_detected:
                            timer_detected = True
                            timer_start_time = time.perf_counter()
                            timer_initial_seconds = curr_secs
                            timer_source = str(dom_timer.get("source", "dom"))
                            critical_trace.mark(
                                "solver.timer.detected", event="state",
                                seconds=timer_initial_seconds, source=timer_source,
                            )
                            telemetry_bus.record(
                                level="INFO",
                                subsystem="engine:captcha",
                                message=f"{pfx}[TIMER_DETECTED] Countdown timer detected: {timer_initial_seconds}s (source: {timer_source})",
                                context={**t_ctx, "seconds": timer_initial_seconds, "source": timer_source},
                                tier="engine",
                            )

                        if timer_start_time is not None:
                            elapsed_timer = time.perf_counter() - timer_start_time
                            computed_remaining = max(0, timer_initial_seconds - int(elapsed_timer))
                            remaining = min(computed_remaining, curr_secs) if curr_secs > 0 else computed_remaining
                        else:
                            remaining = curr_secs

                        if remaining != last_notified_sec:
                            last_notified_sec = remaining
                            self._notify_stage(task_id, "countdown", {"countdown_seconds": remaining})
                            telemetry_bus.record(
                                level="DEBUG",
                                subsystem="engine:captcha",
                                message=f"{pfx}[TIMER_TICK] Countdown: {remaining}s remaining",
                                context={**t_ctx, "countdown_seconds": remaining},
                                tier="engine",
                            )
                            if remaining == 0:
                                critical_trace.mark(
                                    "solver.timer.satisfied", event="state", source=timer_source,
                                )
                                telemetry_bus.record(
                                    level="INFO",
                                    subsystem="engine:captcha",
                                    message=f"{pfx}[TIMER_COMPLETED] Countdown reached 0s",
                                    context={**t_ctx},
                                    tier="engine",
                                )
                except Exception as timer_exc:
                    telemetry_bus.record(
                        level="WARN",
                        subsystem="engine:captcha",
                        message=f"{pfx}[TIMER_DETECT_FAILED] {timer_exc}",
                        context={**t_ctx, "url": url},
                        tier="engine",
                    )
                try:
                    val = await page.evaluate("""() => {
                        let el = document.querySelector('input[name=cf-turnstile-response], [name=cf-turnstile-response]');
                        return el ? el.value : null;
                    }""")
                    if val and len(val.strip()) > 10:
                        turnstile_token = val.strip()
                except Exception:
                    pass

                # If Turnstile token not yet acquired, attempt to click challenge checkbox in iframe
                if not turnstile_token:
                    for cf in [f for f in page.frames if "challenges.cloudflare.com" in f.url]:
                        try:
                            box = await cf.query_selector("input[type=checkbox], #challenge-stage, .ctp-checkbox-label")
                            if box:
                                await box.click()
                        except Exception:
                            pass

                has_turnstile_widget = False
                try:
                    has_turnstile_widget = bool(await page.query_selector(".cf-turnstile, [name=cf-turnstile-response], iframe[src*='challenges.cloudflare.com']"))
                except Exception:
                    pass

                if has_turnstile_widget and not saw_turnstile:
                    saw_turnstile = True
                    critical_trace.mark("solver.turnstile.detected", event="state")
                    self._notify_stage(task_id, "turnstile_detected", {})
                    telemetry_bus.record(
                        level="INFO",
                        subsystem="engine:captcha",
                        message=f"{pfx}[SOLVER_WIDGET_FOUND] Turnstile widget detected on page ({elapsed:.1f}s)",
                        context={**t_ctx, "url": url, "engine": engine_name, "elapsed_s": round(elapsed, 1)},
                        tier="engine",
                    )

                if turnstile_token and token_acquired_at is None:
                    token_acquired_at = elapsed
                    critical_trace.mark(
                        "solver.turnstile.solved", event="state", elapsed_seconds=elapsed,
                    )
                    self._notify_stage(task_id, "turnstile_solved", {})
                    telemetry_bus.record(
                        level="INFO",
                        subsystem="engine:captcha",
                        message=(
                            f"{pfx}[SOLVER_TOKEN_ACQUIRED] Turnstile token acquired"
                            + ("; provider continuation will submit it" if completion_mode == "token" else ", awaiting direct URL/countdown...")
                        ),
                        context={
                            **t_ctx,
                            "elapsed_s": round(elapsed, 1),
                            "engine": engine_name,
                            "completion_mode": completion_mode,
                        },
                        tier="engine",
                    )

                # A resumable provider already owns the original step-two form,
                # cookies and countdown deadline. Its browser lane exists only to
                # acquire a fresh challenge token; waiting here for the browser to
                # rediscover a direct URL duplicates the provider operation and
                # serialized multipart packages by another 15-30 seconds.
                if turnstile_token and completion_mode == "token":
                    resolved = True
                    harvested_cookies = cookies
                    critical_trace.mark(
                        "solver.token_completion", event="state",
                        elapsed_seconds=elapsed,
                    )
                    break

                if saw_turnstile and not captured_direct_url:
                    try:
                        dl_el = await page.query_selector("#download-link[href], a[id='download-link'][href]")
                        if dl_el:
                            href = await dl_el.get_attribute("href")
                            if href and href.startswith("http") and not is_ad_domain(href):
                                captured_direct_url = urllib.parse.unquote(href)
                                self._notify_stage(task_id, "primed", {"direct_url": captured_direct_url})
                                telemetry_bus.record(
                                    level="INFO",
                                    subsystem="engine:captcha",
                                    message=f"{pfx}[SOLVER_DOM_LINK] Captured direct URL via DOM #download-link",
                                    context={**t_ctx, "direct_url": captured_direct_url[:120], "engine": engine_name},
                                    tier="engine",
                                )
                                resolved = True
                                harvested_cookies = cookies
                                break
                    except Exception:
                        pass

                # If on Step 2, click download/start button once conditions are met:
                # - If Turnstile challenge present: token must be acquired AND countdown reached 0 (or no timer)
                # - If no Turnstile challenge: countdown reached 0 (or no timer)
                can_click_button = False
                if not clicked_step2_button:
                    can_click_button = step2_click_ready(
                        has_token=bool(turnstile_token),
                        timer_detected=timer_detected,
                        timer_remaining=last_notified_sec,
                        saw_step_advance=saw_step_advance,
                        has_widget=has_turnstile_widget or saw_turnstile,
                    )

                if can_click_button:
                    # Strip invisible ad overlay traps before clicking
                    try:
                        await page.evaluate("""() => {
                            document.querySelectorAll('div[style*="position: fixed"], div[style*="z-index: 2147483647"]').forEach(el => {
                                if (!el.querySelector('download-countdown, button, form')) el.remove();
                            });
                        }""")
                    except Exception:
                        pass
                    try:
                        btn_ready = await page.evaluate("""() => {
                            let b = Array.from(document.querySelectorAll('button')).find(
                                x => (x.innerText || '').includes('Download') || (x.innerText || '').includes('Start')
                            );
                            if (b && !b.disabled) {
                                b.click();
                                return true;
                            }
                            return false;
                        }""")
                        if btn_ready:
                            clicked_step2_button = True
                            critical_trace.mark(
                                "solver.step2.click", event="state",
                                timer_detected=timer_detected,
                                timer_remaining=last_notified_sec,
                                has_turnstile_token=bool(turnstile_token),
                            )
                            self._notify_stage(task_id, "step2_clicked", {})
                            telemetry_bus.record(
                                level="INFO",
                                subsystem="engine:captcha",
                                message=f"{pfx}[SOLVER_STEP2_CLICK] Clicked active download button on Step 2 (Turnstile={bool(turnstile_token)}, Timer={last_notified_sec if last_notified_sec is not None else 'none'})",
                                context={
                                    **t_ctx,
                                    "url": url,
                                    "engine": engine_name,
                                    "timer_detected": timer_detected,
                                    "timer_source": timer_source,
                                    "timer_remaining": last_notified_sec,
                                },
                                tier="engine",
                            )
                    except Exception:
                        pass

                if not saw_cloudflare_interstitial and ("just a moment" in title.lower() or "cloudflare" in title.lower() or "checking your browser" in title.lower()):
                    saw_cloudflare_interstitial = True

                if turnstile_token:
                    if captured_direct_url:
                        resolved = True
                        harvested_cookies = cookies
                        break
                    # Check if page has countdown or step-advance form in progress
                    has_countdown = timer_detected and (last_notified_sec is not None and last_notified_sec > 0)
                    wait_limit = 15.0 if (has_countdown or saw_step_advance) else 3.5
                    if (elapsed - token_acquired_at) >= wait_limit or elapsed >= (timeout_seconds - 1.0):
                        resolved = True
                        harvested_cookies = cookies
                        break
                elif has_turnstile_widget or saw_turnstile or saw_step_advance:
                    await asyncio.sleep(0.3)
                    continue
                elif "cf_clearance" in cookies:
                    resolved = True
                    harvested_cookies = cookies
                    break
                elif saw_cloudflare_interstitial and ("Just a moment" not in title and "Cloudflare" not in title and title != ""):
                    resolved = True
                    harvested_cookies = cookies
                    break
                elif elapsed >= 5.0 and not saw_turnstile and not saw_step_advance and not has_turnstile_widget:
                    resolved = True
                    harvested_cookies = cookies
                    break

                await asyncio.sleep(0.35)

            dur = (time.perf_counter() - t0) * 1000.0

            if resolved:
                ua_val = ""
                try:
                    ua_val = await page.evaluate("navigator.userAgent")
                except Exception:
                    pass
                clearance_cache.set_clearance(
                    url,
                    harvested_cookies,
                    user_agent=ua_val,
                )
                if captured_direct_url and not direct_url_matches_request(url, captured_direct_url):
                    telemetry_bus.record(
                        level="ERROR",
                        subsystem="engine:captcha",
                        message=f"{pfx}[SOLVER_LINK_MISMATCH] Captured a direct URL for a different file; "
                                f"discarding (wanted '{_url_filename(url)}', got '{_url_filename(captured_direct_url)}')",
                        context={**t_ctx, "url": url, "direct_url": captured_direct_url[:160],
                                 "engine": engine_name},
                        tier="engine",
                    )
                    return {
                        "success": False,
                        "engine": engine_name,
                        "cookies": harvested_cookies,
                        "user_agent": ua_val,
                        "error": "Solver captured a direct URL belonging to a different file",
                    }
                telemetry_bus.record(
                    level="INFO",
                    subsystem="engine:captcha",
                    message=f"{pfx}[CHALLENGE_SOLVED] Passed challenge on {url} in {dur:.1f}ms ({engine_name})"
                           + (f" — direct URL captured" if captured_direct_url else ""),
                    context={
                        **t_ctx,
                        "url": url,
                        "engine": engine_name,
                        "title": final_title,
                        "cookies": list(harvested_cookies.keys()),
                        "has_turnstile_token": bool(turnstile_token),
                        "has_direct_url": bool(captured_direct_url),
                        "saw_turnstile": saw_turnstile,
                    },
                    duration_ms=dur,
                    tier="engine",
                )
                return {
                    "success": True,
                    "engine": engine_name,
                    "cookies": harvested_cookies,
                    "user_agent": ua_val,
                    "title": final_title,
                    "url": page.url,
                    "turnstile_token": turnstile_token,
                    "direct_url": captured_direct_url,
                    "duration_ms": dur,
                    "timer": {
                        "detected": timer_detected,
                        "seconds": timer_initial_seconds,
                        "source": timer_source,
                        "remaining": last_notified_sec if last_notified_sec is not None else 0,
                    },
                }
            else:
                telemetry_bus.record(
                    level="WARN",
                    subsystem="engine:captcha",
                    message=f"{pfx}[CHALLENGE_TIMEOUT] Solver {engine_name} timed out on {url} ({final_title})",
                    context={**t_ctx, "url": url, "engine": engine_name, "title": final_title, "saw_turnstile": saw_turnstile},
                    tier="engine",
                )
                return {
                    "success": False,
                    "engine": engine_name,
                    "error": f"Challenge resolution timed out ({final_title})",
                    "timer": {
                        "detected": timer_detected,
                        "seconds": timer_initial_seconds,
                        "source": timer_source,
                        "remaining": last_notified_sec if last_notified_sec is not None else 0,
                    },
                }

        finally:
            try:
                await page.close()
            except Exception:
                pass
            try:
                await ctx.close()
            except Exception:
                pass
            self._last_used = time.time()

    async def _execute_solve_on_actor(
        self,
        url: str,
        timeout_seconds: float = 90.0,
        initial_cookies: Optional[dict[str, str]] = None,
        task_id: Optional[str] = None,
        completion_mode: str = "direct_or_token_wait",
    ) -> dict[str, Any]:
        """Actor thread execution hook for challenge resolution."""
        return await self._async_execute_solve(
            url, timeout_seconds, initial_cookies, task_id, completion_mode,
        )

    async def _dispatch_solve(
        self,
        url: str,
        timeout_seconds: float,
        initial_cookies: Optional[dict[str, str]],
        task_id: Optional[str],
        completion_mode: str,
        future: concurrent.futures.Future,
        trace_identity: critical_trace.TraceIdentity,
        route_proxy: str | None = None,
    ) -> None:
        self._active_solves += 1
        self._last_used = time.time()
        # This coroutine runs as its own Task on the actor loop: binding here
        # scopes the caller's route to this one solve.
        route_http.bind_task_route(route_proxy)
        try:
            with critical_trace.bind(
                package_id=trace_identity.package_id,
                task_id=trace_identity.task_id or task_id,
                part_number=trace_identity.part_number,
                attempt_id=trace_identity.attempt_id,
                span_id=trace_identity.span_id,
                parent_span_id=trace_identity.parent_span_id,
            ):
                if not self._lane_semaphore:
                    self._lane_semaphore = asyncio.Semaphore(self.max_lanes)
                critical_trace.mark(
                    "solver.lane.queued", event="state", resource="solver_lane",
                    max_lanes=self.max_lanes,
                )
                package_id = trace_identity.package_id or ""
                if package_id:
                    self._package_solve_demand[package_id] = self._package_solve_demand.get(package_id, 0) + 1
                queued_at_ns = time.perf_counter_ns()
                async with self._lane_semaphore:
                    queue_wait_seconds = (time.perf_counter_ns() - queued_at_ns) / 1_000_000_000
                    demand = self._package_solve_demand.get(package_id, 1) if package_id else 1
                    recommended = self._lane_policy.recommended_limit(
                        current_limit=self.max_lanes,
                        package_demand=demand,
                        queue_wait_seconds=queue_wait_seconds,
                    )
                    if recommended > self.max_lanes:
                        added = recommended - self.max_lanes
                        self.max_lanes = recommended
                        for _ in range(added):
                            self._lane_semaphore.release()
                        critical_trace.mark(
                            "solver.lane.expanded", event="state", resource="solver_lane",
                            max_lanes=self.max_lanes, package_demand=demand,
                            queue_wait_seconds=queue_wait_seconds,
                        )
                    with critical_trace.span("solver.solve", resource="solver_lane"):
                        res_coro_or_val = self._execute_solve_on_actor(
                            url, timeout_seconds, initial_cookies, task_id, completion_mode,
                        )
                        if asyncio.iscoroutine(res_coro_or_val):
                            res = await res_coro_or_val
                        else:
                            res = res_coro_or_val
                        critical_trace.mark(
                            "solver.result", event="state", success=bool(res.get("success")),
                            engine=res.get("engine"), has_direct_url=bool(res.get("direct_url")),
                        )
                        if not future.done():
                            future.set_result(res)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
        finally:
            package_id = trace_identity.package_id or ""
            if package_id:
                remaining = self._package_solve_demand.get(package_id, 1) - 1
                if remaining > 0:
                    self._package_solve_demand[package_id] = remaining
                else:
                    self._package_solve_demand.pop(package_id, None)
            self._active_solves -= 1
            self._last_used = time.time()

    def solve_challenge_sync(
        self,
        url: str,
        timeout_seconds: float = 90.0,
        initial_cookies: Optional[dict[str, str]] = None,
        task_id: Optional[str] = None,
        completion_mode: str = "direct_or_token_wait",
        route_proxy: Any = route_http.ACTIVE,
    ) -> dict[str, Any]:
        """Submit challenge job to pinned actor queue and wait on future."""
        # Captured on the caller's thread, where the task's route is bound;
        # clearance cookies are tied to the IP that earned them.
        if route_proxy is route_http.ACTIVE:
            route_proxy = route_http.active_proxy()
        self._ensure_actor_started()
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("BrowserSolver actor event loop is not running")

        future = concurrent.futures.Future()
        trace_identity = critical_trace.current_identity()
        critical_trace.mark("solver.submitted", event="state", resource="solver_lane")
        asyncio.run_coroutine_threadsafe(
            self._dispatch_solve(
                url, timeout_seconds, initial_cookies, task_id, completion_mode,
                future, trace_identity, route_proxy,
            ),
            self._loop,
        )
        try:
            return future.result(timeout=timeout_seconds + 15.0)
        except concurrent.futures.TimeoutError:
            return {
                "success": False,
                "engine": self._active_engine or "actor_queue",
                "error": f"Solver actor queue wait timed out after {timeout_seconds + 15.0:.1f}s",
            }

    async def solve_challenge_async(
        self,
        url: str,
        timeout_seconds: float = 90.0,
        initial_cookies: Optional[dict[str, str]] = None,
        task_id: Optional[str] = None,
        completion_mode: str = "direct_or_token_wait",
    ) -> dict[str, Any]:
        """Asynchronously await challenge resolution from the pinned actor queue."""
        self._ensure_actor_started()
        loop = asyncio.get_running_loop()
        # run_in_executor does not carry context, so the route goes explicitly.
        return await loop.run_in_executor(
            None,
            self.solve_challenge_sync,
            url,
            timeout_seconds,
            initial_cookies,
            task_id,
            completion_mode,
            route_http.active_proxy(),
        )

    def follow_download_sync(self, url: str, timeout_seconds: float = 150.0) -> dict[str, Any]:
        """Click through from `url` to the file it leads to (see button_follower)."""
        route_proxy = route_http.active_proxy()  # captured here, where the caller's route is bound
        self._ensure_actor_started()
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("BrowserSolver actor event loop is not running")
        future = asyncio.run_coroutine_threadsafe(self._async_follow(url, route_proxy), self._loop)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return {"start_url": url, "final_url": url, "direct_url": None, "hops": [], "rejected": [],
                    "captcha": False, "error": f"gave up after {timeout_seconds:.0f}s"}

    async def _async_follow(self, url: str, route_proxy: str | None) -> dict[str, Any]:
        from .button_follower import follow
        route_http.bind_task_route(route_proxy)
        browser, _engine = await self._ensure_browser()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True,
                                        proxy=route_http.playwright_proxy())
        try:
            page = await ctx.new_page()
            blocker = _ad_blocker()
            # Pop-ups on file hosts are ads; the real next step opens in place.
            ctx.on("page", lambda popup: asyncio.ensure_future(popup.close()) if popup != page else None)
            return (await follow(page, url, blocker)).to_dict()
        finally:
            await ctx.close()

    def solve(
        self,
        url: str,
        timeout_seconds: float = 45.0,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Public alias for solve_challenge_sync."""
        return self.solve_challenge_sync(url, timeout_seconds=timeout_seconds, task_id=task_id)

    def reap(self) -> None:
        """Trigger immediate cleanup of warm browser session."""
        if self._loop and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._async_reap_browser(), self._loop)
            try:
                fut.result(timeout=5.0)
            except Exception:
                pass

    def close(self) -> None:
        """Shut down the solver actor daemon."""
        if self._stop_event and self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._actor_thread and self._actor_thread.is_alive():
            self._actor_thread.join(timeout=5.0)


BrowserSolver = BrowserSolverDaemon
solver_daemon = BrowserSolverDaemon()
