"""Following a download button to the file, in the browser.

File hosts put two to five pages between a post and its file: a mirror page,
a "free download" button, a countdown, a "get link" button. The follower
walks them the way a person would: hide the ads (filter lists), wait out any
countdown, score the clickable elements with the same ElementScorer the crawl
uses, click the best one, and stop when the browser is handed a file. Every
candidate passed over is recorded with the reason, so a wrong turn can be
explained rather than guessed at.
"""
from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

from .dom_cleaner import CleanedElement
from .element_scorer import ElementScorer
from .telemetry import telemetry_bus

MIN_SCORE = 30
# Words of the in-between steps. The crawl scorer ranks which link on a post is
# the file and scores these low (no file-like URL, often no URL at all); on a
# step page they are exactly the way forward.
STEP_BONUS = 30
_STEP_WORDS = re.compile(r"\b(continue|proceed|get (the )?link|generate( download)? link|create download link|"
                         r"free download|slow download|regular download|go to (the )?download|download (now|file)|"
                         r"download from|mirror \d*|click here to download)\b", re.I)
MAX_STEPS = 6
COUNTDOWN_WAIT = 45.0
_COUNTDOWN_TEXT = re.compile(r"(please wait|wait \d+|\d+\s*(s|sec|secs|seconds)\b|generating (your )?link|countdown)", re.I)
_CAPTCHA_FRAMES = ("challenges.cloudflare.com", "google.com/recaptcha", "recaptcha.net", "hcaptcha.com")
_FILE_TYPES = ("application/octet-stream", "application/zip", "application/x-zip", "application/x-7z", "application/x-rar",
               "application/vnd.rar", "application/x-msdownload", "application/x-bittorrent", "application/pdf",
               "application/x-iso", "application/x-tar", "application/gzip", "video/", "audio/")

_CANDIDATES = """() => {
  const out = [];
  const els = document.querySelectorAll('a[href], button, input[type=submit], input[type=button], [role=button], [onclick]');
  let index = 0;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    el.setAttribute('data-tm-candidate', String(index));
    out.push({
      index: index++, tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').trim().slice(0, 120),
      href: el.href || '', id: el.id || '', classes: [...el.classList],
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      visible: r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none' && parseFloat(st.opacity || '1') > 0.1,
      area: r.width * r.height,
    });
  }
  return out;
}"""


@dataclass
class Step:
    input_url: str
    output_url: str
    strategy: str
    captcha_encountered: bool = False
    error: str | None = None


@dataclass
class FollowResult:
    start_url: str
    final_url: str = ""
    direct_url: str | None = None
    filename: str | None = None
    hops: list[Step] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    captcha: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        # Same hop shape as the shortlink resolver, so Explore draws both alike.
        for number, hop in enumerate(out["hops"], 1):
            hop["hop_number"] = number
            hop["status"] = "captcha" if hop["captcha_encountered"] else "error" if hop["error"] else "ok"
        return out


def is_file_response(headers: dict[str, str]) -> str | None:
    """The filename (or "" when unnamed) if this response is a file, else None."""
    disposition = headers.get("content-disposition", "")
    if "attachment" in disposition.lower():
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
        return urllib.parse.unquote(match.group(1)) if match else ""
    content_type = headers.get("content-type", "").lower()
    if content_type and not content_type.startswith("text/") and any(content_type.startswith(t) for t in _FILE_TYPES):
        return ""
    return None


def pick(candidates: list[dict[str, Any]], page_url: str, tried: set[str], blocked: set[str]) -> tuple[dict | None, list[dict]]:
    """The best candidate to click, and every other one with why it was passed over."""
    best, best_key, rejected = None, (-1, 0.0), []
    for cand in candidates:
        label = cand["text"] or cand["href"] or cand["id"] or cand["tag"]
        key = f"{cand['href']}|{cand['text']}"
        if not cand["visible"]:
            reason = "hidden"
        elif key in tried:
            reason = "already clicked"
        elif cand["href"] and cand["href"] in blocked:
            reason = "ad (filter lists)"
        else:
            element = CleanedElement(tag=cand["tag"] if cand["tag"] in {"a", "button"} else "button", text=cand["text"],
                                     target_url=cand["href"], element_id=cand["id"], element_classes=cand["classes"])
            score = ElementScorer.score_element(element, page_url).score
            if _STEP_WORDS.search(cand["text"]):
                score += STEP_BONUS
            if score < MIN_SCORE:
                reason = f"score {score} below {MIN_SCORE}"
            else:
                if (score, cand["area"]) > best_key:
                    if best is not None:
                        rejected.append({"text": best["text"][:60], "url": best["href"], "reason": f"outscored ({best_key[0]} < {score})"})
                    best, best_key = cand, (score, cand["area"])
                    best["_key"] = key
                else:
                    rejected.append({"text": label[:60], "url": cand["href"], "reason": f"outscored ({score} < {best_key[0]})"})
                continue
        rejected.append({"text": label[:60], "url": cand["href"], "reason": reason})
    return best, rejected


async def _has_captcha(page: Any) -> bool:
    return any(any(host in (frame.url or "") for host in _CAPTCHA_FRAMES) for frame in page.frames)


async def _wait_countdown(page: Any) -> None:
    """While the page says to wait, wait (bounded): its button is not ready yet."""
    deadline = time.monotonic() + COUNTDOWN_WAIT
    while time.monotonic() < deadline:
        text = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 20000)")
        if not _COUNTDOWN_TEXT.search(text):
            return
        await asyncio.sleep(1.0)


async def _probe(page: Any, request: Any) -> str | None:
    """Ask for one byte of a GET navigation, with the browser's cookies: a file
    answers with file headers. The browser is never handed the file itself."""
    from . import http_client
    cookies = await page.context.cookies(request.url)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"range", "cookie"}}
    if cookies:
        headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    headers["Range"] = "bytes=0-0"

    def one_byte() -> dict[str, str]:
        response = http_client.request("GET", request.url, headers=headers, timeout=20, stream=True)
        try:
            return {k.lower(): v for k, v in response.headers.items()}
        finally:
            response.close()

    return is_file_response(await asyncio.to_thread(one_byte))


def _leaf(url: str) -> str | None:
    return urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]) or None


async def follow(page: Any, start_url: str, blocker: Any = None, *, max_steps: int = MAX_STEPS) -> FollowResult:
    """Click through from `start_url` until a navigation leads to a file."""
    from .browser_solver import hide_ads, list_verdicts

    result = FollowResult(start_url=start_url)
    found: dict[str, Any] = {}
    found_event = asyncio.Event()
    blocked_request = list_verdicts(page, blocker, start_url)
    started = False

    async def handler(route: Any) -> None:
        request = route.request
        if await blocked_request(request):
            await route.abort("blockedbyclient")
            return
        if started and request.is_navigation_request() and request.frame == page.main_frame:
            if request.method == "GET":
                name = await _probe(page, request)
                if name is not None:
                    found.update(url=request.url, filename=name or _leaf(request.url))
                    found_event.set()
                    await route.abort("aborted")
                    return
            else:
                # A form post: see its answer without following redirects, so a
                # file behind it is never pulled whole into memory.
                response = await route.fetch(max_redirects=0)
                name = None if 300 <= response.status < 400 else is_file_response({k.lower(): v for k, v in response.headers.items()})
                if name is not None:
                    found.update(url=request.url, filename=name or _leaf(request.url), method="POST")
                    found_event.set()
                    await route.abort("aborted")
                    return
                await route.fulfill(response=response)
                return
        await route.continue_()

    await page.route("**/*", handler)
    await page.goto(start_url, wait_until="domcontentloaded")
    started = True
    tried: set[str] = set()
    for _ in range(max_steps):
        if found:
            break
        if await _has_captcha(page):
            result.captcha = True
            result.hops.append(Step(page.url, page.url, "stopped at a captcha", captcha_encountered=True))
            break
        if blocker is not None:
            await hide_ads(page, blocker)
        await _wait_countdown(page)
        candidates = await page.evaluate(_CANDIDATES)
        hrefs = [c["href"] for c in candidates if c["href"]]
        blocked = await asyncio.to_thread(blocker.blocked_urls, hrefs) if blocker is not None and hrefs else set()
        best, rejected = pick(candidates, page.url, tried, blocked)
        result.rejected.extend(r | {"page": page.url} for r in rejected[:40])
        telemetry_bus.record(level="INFO", subsystem="engine:button_follower",
                             message=f"[FOLLOW_STEP] {page.url} -> {best['text'][:40] if best else 'nothing to click'}",
                             context={"page": page.url, "chosen": best and best["text"], "candidates": len(candidates),
                                      "rejected": rejected[:20]}, tier="engine")
        if best is None:
            result.error = "no download button left on the page"
            break
        tried.add(best["_key"])
        before = page.url
        # Wait for whichever comes first: the next page, or a navigation that was a file.
        loaded = asyncio.ensure_future(page.wait_for_event("load", timeout=10_000))
        got_file = asyncio.ensure_future(found_event.wait())
        try:
            await page.locator(f'[data-tm-candidate="{best["index"]}"]').click(timeout=5000, no_wait_after=True)
            await asyncio.wait({loaded, got_file}, timeout=11, return_when=asyncio.FIRST_COMPLETED)
            error = None
        except Exception as exc:
            error = str(exc).splitlines()[0][:160]
        finally:
            for waiter in (loaded, got_file):
                waiter.cancel()
            await asyncio.gather(loaded, got_file, return_exceptions=True)
        result.hops.append(Step(before, found.get("url") or page.url, f"clicked “{best['text'][:40] or 'button'}”", error=error))
    result.final_url = found.get("url") or page.url
    result.direct_url = found.get("url")
    result.filename = found.get("filename")
    if not result.direct_url and not result.error and not result.captcha:
        result.error = f"no file after {max_steps} steps"
    return result
