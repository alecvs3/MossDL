from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable

from .captcha import CaptchaChallenge, CaptchaManager, CaptchaType
from .shortlinks import _TargetParser, _is_resource_candidate, _safe_url
from .flaresolverr_client import FlareSolverrClient
from .bypass_vip import BypassVipClient
from .shortlink_rules import DeepParamDecoder, SpecializedRuleRegistry
from .telemetry import telemetry_bus
from . import route_http



def is_safe_external_url(url: str) -> bool:
    return _safe_url(url, url) is not None and not _is_resource_candidate(url, url)


logger = logging.getLogger(__name__)

_KNOWN_STORAGE_HOSTS = {
    "mediafire.com", "gofile.io", "mega.nz", "pixeldrain.com", "1fichier.com",
    "rapidgator.net", "fileboom.me", "keep2share.cc", "transfer.it",
    "drive.google.com", "dropbox.com", "github.com", "gitlab.com",
    "vikingfile.com", "vik1ngfile.site", "cyberdrop.me", "cyberdrop.to",
    "cyberfile.me", "filester.me", "iceyfile.com", "koofr.eu", "koofr.net", "k00.fr", "krakenfiles.com",
    "fileditch.com", "fileditchfiles.me", "theditch.st", "imglike.com", "imagepond.net",
    "imgbb.com", "ibb.co", "catbox.moe", "litter.catbox.moe", "files.catbox.moe", "fatbox.moe",
    "upload.ee", "bunkr.cr", "bunkr.site", "bunkr.ph", "bunkr.is", "bunkr.black", "bunkr.media", "bunkr.red",
    "anontransfer.com", "pcloud.com", "pcloud.link", "pc.cd", "e.pc.cd", "u.pc.cd",
    "1drv.ms", "onedrive.live.com", "box.com", "app.box.com", "postimg.cc", "postimg.org", "postimages.org",
    "imgbox.com", "imagebam.com", "imx.to", "imagevenue.com", "pixhost.to", "pixhost.cc", "pixhost.org",
    "imgur.com",
    "streamable.com", "sendvid.com", "whyp.it", "buzzheavier.com",
    "pillowcase.su", "pillowcase.zip", "pillows.su", "mixdrop.co", "mixdrop.to", "mixdrop.sx",
    "doodstream.com", "dood.so", "dood.to", "streamtape.com", "streamtape.to", "voe.sx",
    "fuckingfast.co", "webmshare.com", "send.now", "giphy.com", "clyp.it", "bandcamp.com", "vipr.im",
    "wetransfer.com", "we.tl", "cloud.mail.ru", "disk.yandex.com", "disk.yandex.ru", "disk.yandex.com.tr", "yadi.sk",
    "rootz.so", "gupload.xyz", "gupload.to", "archive.org", "nova.storage", "flickr.com", "vsco.co",
    "twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com", "tiktok.com", "pinterest.com", "pin.it",
    "odysee.com", "rumble.com", "dailymotion.com", "dai.ly", "twitch.tv"
}
_DIRECT_FILE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2", ".iso", ".bin", ".exe", ".dmg",
    ".mp4", ".mkv", ".mp3", ".flac", ".apk", ".msi", ".001", ".002", ".003", ".004", ".005",
    ".r00", ".r01", ".z01", ".z02"
}


@dataclass
class HopResult:
    hop_number: int
    input_url: str
    output_url: str
    strategy: str
    delay_seconds: float = 0.0
    captcha_encountered: bool = False
    status: str = "resolved"
    error: str | None = None


class _FormExtractor(HTMLParser):
    """Extracts form actions, hidden inputs, and countdown timer metadata."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current_form: dict[str, Any] | None = None
        self.countdown_delay: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        tag_lower = tag.lower()

        if tag_lower == "form":
            self._current_form = {
                "action": attributes.get("action", ""),
                "method": attributes.get("method", "POST").upper(),
                "inputs": {},
            }
        elif tag_lower == "input" and self._current_form is not None:
            name = attributes.get("name")
            val = attributes.get("value", "")
            if name:
                self._current_form["inputs"][name] = val

        # Detect countdown delay in data attributes or tags
        for k, v in attributes.items():
            if v and ("delay" in k.lower() or "timer" in k.lower() or "seconds" in k.lower()):
                try:
                    num = int(re.search(r"\d+", v).group())
                    if 0 < num <= 30:
                        self.countdown_delay = max(self.countdown_delay, num)
                except Exception:
                    pass

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._current_form is not None:
            self.forms.append(self._current_form)
            self._current_form = None


class RecursiveShortlinkResolver:
    """
    Advanced recursive shortlink unraveller.
    Traverses multi-hop redirect chains, unpackages embedded parameters,
    respects countdown timers, and coordinates anti-bot CAPTCHAs.
    """

    def __init__(
        self,
        captcha_manager: CaptchaManager | None = None,
        flaresolverr: FlareSolverrClient | None = None,
        bypass_vip: BypassVipClient | None = None,
        max_hops: int = 8,
        request_timeout: float = 12.0,
        user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    ) -> None:
        self.captcha_manager = captcha_manager
        self.flaresolverr = flaresolverr or FlareSolverrClient()
        self.bypass_vip = bypass_vip or BypassVipClient()
        self.max_hops = max_hops
        self.request_timeout = request_timeout
        self.user_agent = user_agent
        self.http_fetcher: Callable[..., tuple[int, str, dict[str, str], str]] | None = None  # Hook for mocks
        self.rules = SpecializedRuleRegistry(user_agent=self.user_agent, request_timeout=self.request_timeout)
        self.param_decoder = DeepParamDecoder()

    async def resolve_chain(
        self,
        start_url: str,
        task_id: str | None = None,
        progress_callback: Callable[[HopResult], None] | None = None,
        max_hops: int | None = None,
        method: str = "GET",
        form_data: dict[str, str] | None = None,
    ) -> tuple[str, list[HopResult]]:
        """
        Recursively resolves start_url until final direct or file host URL is reached.
        Supports initial GET or POST (e.g. safelink form submission) requests.
        Returns (final_url, list_of_hop_results).
        """
        current_url = start_url.strip()
        hops: list[HopResult] = []

        # If start_url is already a direct archive/media file or terminal file host, return immediately
        if self._is_terminal_target(current_url):
            telemetry_bus.record(
                level="INFO",
                subsystem="provider:shortlink",
                message=f"URL is already terminal target, skipping resolution: {current_url}",
                context={"start_url": current_url},
            )
            return current_url, []

        visited_urls: set[str] = set()
        hops_limit = max_hops or self.max_hops
        telemetry_bus.record(
            level="INFO",
            subsystem="provider:shortlink",
            message=f"Starting shortlink resolution chain for {start_url}",
            context={"start_url": start_url, "max_hops": hops_limit},
        )

        for hop_index in range(1, hops_limit + 1):

            normalized_key = self._normalize_key(current_url)
            if normalized_key in visited_urls:
                hop = HopResult(
                    hop_number=hop_index,
                    input_url=current_url,
                    output_url=current_url,
                    strategy="loop_detected",
                    status="loop_detected",
                    error=f"Circular redirect loop detected at {current_url}",
                )
                hops.append(hop)
                if progress_callback:
                    progress_callback(hop)
                break

            visited_urls.add(normalized_key)

            # Check if initial hop is a POST action (e.g. safelink form submission)
            if hop_index == 1 and method.upper() == "POST" and form_data:
                try:
                    fetch_res = await self._fetch(current_url, method="POST", post_data=form_data)
                    status_code, content_type, headers, body, final_url = fetch_res
                    dest = self._extract_post_response_target(body, current_url)
                    next_url = dest or (final_url if final_url != current_url else None)
                    if next_url and is_safe_external_url(next_url):
                        hop = HopResult(
                            hop_number=hop_index,
                            input_url=current_url,
                            output_url=next_url,
                            strategy="safelink_post_extract" if dest else "form_post_submit",
                        )
                        hops.append(hop)
                        if progress_callback:
                            progress_callback(hop)
                        current_url = next_url
                        continue
                except Exception as exc:
                    hop = HopResult(
                        hop_number=hop_index,
                        input_url=current_url,
                        output_url=current_url,
                        strategy="form_post_error",
                        status="aborted",
                        error=str(exc),
                    )
                    hops.append(hop)
                    if progress_callback:
                        progress_callback(hop)
                    break

            # Check if current_url is already a final file host or direct media file
            if self._is_terminal_target(current_url):
                break

            # 1. Tier 0: Deep Parameter & Encoding Decoder (Offline < 1ms)
            unpacked = self.param_decoder.decode(current_url) or self._extract_embedded_param_url(current_url)
            if unpacked and unpacked != current_url and is_safe_external_url(unpacked):
                hop = HopResult(
                    hop_number=hop_index,
                    input_url=current_url,
                    output_url=unpacked,
                    strategy="param_extract",
                )
                hops.append(hop)
                if progress_callback:
                    progress_callback(hop)
                current_url = unpacked
                continue

            # 2. Tier 1: Native Declarative Bypasser Rules (FastForward / AdsBypasser)
            rule_target = self.rules.match_and_bypass(current_url)
            if rule_target and rule_target != current_url and is_safe_external_url(rule_target):
                hop = HopResult(
                    hop_number=hop_index,
                    input_url=current_url,
                    output_url=rule_target,
                    strategy="rule_rewrite",
                )
                hops.append(hop)
                if progress_callback:
                    progress_callback(hop)
                current_url = rule_target
                continue

            # Specialized API hook (AdShrink / shrink-service prototype API)
            if ("adshnk.com" in current_url or "ashnk.com" in current_url) and not current_url.endswith(".html"):
                dest = await asyncio.to_thread(self.rules.query_adshrink_api, current_url)
                if dest and dest != current_url and is_safe_external_url(dest):
                    hop = HopResult(
                        hop_number=hop_index,
                        input_url=current_url,
                        output_url=dest,
                        strategy="adshrink_api_resolve",
                    )
                    hops.append(hop)
                    if progress_callback:
                        progress_callback(hop)
                    current_url = dest
                    continue

            # 3. Tier 2: Bypass.vip Cloud Fallback
            if self.bypass_vip and self.bypass_vip.enabled and hop_index > 0:
                vip_dest = await asyncio.to_thread(self.bypass_vip.resolve, current_url)
                if vip_dest and vip_dest != current_url and is_safe_external_url(vip_dest):
                    hop = HopResult(
                        hop_number=hop_index,
                        input_url=current_url,
                        output_url=vip_dest,
                        strategy="bypass_vip_cloud",
                    )
                    hops.append(hop)
                    if progress_callback:
                        progress_callback(hop)
                    current_url = vip_dest
                    continue

            # 4. Tier 3: HTTP Fetch & Follow
            try:
                fetch_res = await self._fetch(current_url)
                if len(fetch_res) == 5:
                    status_code, content_type, headers, body, final_url = fetch_res
                else:
                    status_code, content_type, headers, body = fetch_res
                    final_url = current_url
            except Exception as exc:
                hop = HopResult(
                    hop_number=hop_index,
                    input_url=current_url,
                    output_url=current_url,
                    strategy="http_fetch_error",
                    status="aborted",
                    error=str(exc),
                )
                hops.append(hop)
                if progress_callback:
                    progress_callback(hop)
                break

            # Handle 3xx Redirect headers or followed redirects
            redirect_header = headers.get("location") or headers.get("Location")
            next_url = None
            if redirect_header:
                next_url = urllib.parse.urljoin(current_url, redirect_header)
            elif final_url and final_url != current_url:
                next_url = final_url

            if next_url and next_url != current_url and is_safe_external_url(next_url):
                parsed_next = urllib.parse.urlsplit(next_url)
                if ("drive.google.com" in (parsed_next.hostname or "")) and ("tab=" in parsed_next.query or parsed_next.path in {"", "/"}):
                    break
                hop = HopResult(
                    hop_number=hop_index,
                    input_url=current_url,
                    output_url=next_url,
                    strategy="http_redirect",
                )
                hops.append(hop)
                if progress_callback:
                    progress_callback(hop)
                current_url = next_url
                continue

            # Check if content is non-HTML (e.g. binary download began directly)
            if "text/html" not in content_type.lower() and "text/plain" not in content_type.lower():
                # Direct media file reached
                break

            # 3. Strategy: Check for Anti-Bot CAPTCHA Challenge in HTML
            captcha_challenge = self._detect_captcha(body, current_url, task_id)
            if captcha_challenge and self.captcha_manager:
                logger.info("Shortlink hop %d hit CAPTCHA [%s]", hop_index, captcha_challenge.captcha_type)
                # Check if FlareSolverr is active and capable of bypassing this challenge
                if self.flaresolverr and self.flaresolverr.is_available():
                    logger.info("Shortlink hop %d routing through FlareSolverr", hop_index)
                    flare_res = await self.flaresolverr.resolve(current_url)
                    if flare_res.status == "ok" and flare_res.url and flare_res.url != current_url:
                        hop = HopResult(
                            hop_number=hop_index,
                            input_url=current_url,
                            output_url=flare_res.url,
                            strategy="flaresolverr_bypass",
                            captcha_encountered=True,
                        )
                        hops.append(hop)
                        if progress_callback:
                            progress_callback(hop)
                        current_url = flare_res.url
                        continue
                try:
                    solution = await self.captcha_manager.request_solution(captcha_challenge)
                    token = solution.get("token") or solution.get("text") or ""
                    # Submit token back to page/form
                    next_url = await self._submit_captcha_token(current_url, body, token)
                    # The page moving on is the site's acceptance; staying put is a rejection.
                    verifier = getattr(self.captcha_manager, "verifier", None)
                    if verifier is not None:
                        if next_url and next_url != current_url:
                            verifier.accept(captcha_challenge.id, "the shortlink moved past the challenge")
                        else:
                            verifier.reject(captcha_challenge.id, "the shortlink stayed on the challenge page")
                    hop = HopResult(
                        hop_number=hop_index,
                        input_url=current_url,
                        output_url=next_url,
                        strategy="captcha_solved",
                        captcha_encountered=True,
                    )
                    hops.append(hop)
                    if progress_callback:
                        progress_callback(hop)
                    current_url = next_url
                    continue
                except Exception as cap_err:
                    hop = HopResult(
                        hop_number=hop_index,
                        input_url=current_url,
                        output_url=current_url,
                        strategy="captcha_failed",
                        status="aborted",
                        error=f"CAPTCHA required: {cap_err}",
                    )
                    hops.append(hop)
                    if progress_callback:
                        progress_callback(hop)
                    break

            # 4. Strategy: Check for Countdown Delay in Intermediate Page
            form_parser = _FormExtractor()
            form_parser.feed(body)
            delay = min(form_parser.countdown_delay, 15)
            if delay > 0:
                logger.info("Shortlink hop %d requires %d seconds delay", hop_index, delay)
                await asyncio.sleep(delay)

            # 5. Strategy: Campaign JSON State Parser (Rekonise / FastForward)
            rekonise_target = self.rules.parse_rekonise_campaign(body, current_url)
            if rekonise_target and rekonise_target != current_url and is_safe_external_url(rekonise_target):
                hop = HopResult(
                    hop_number=hop_index,
                    input_url=current_url,
                    output_url=rekonise_target,
                    strategy="campaign_state_extract",
                )
                hops.append(hop)
                if progress_callback:
                    progress_callback(hop)
                current_url = rekonise_target
                continue

            # 6. Strategy: Target Link or Meta-Refresh Parser
            target_parser = _TargetParser()
            target_parser.feed(body)
            candidate_links = [
                urllib.parse.urljoin(current_url, v) for v in target_parser.values if is_safe_external_url(urllib.parse.urljoin(current_url, v))
            ]

            next_candidate = self._choose_best_target(candidate_links, current_url)
            if next_candidate and next_candidate != current_url:
                hop = HopResult(
                    hop_number=hop_index,
                    input_url=current_url,
                    output_url=next_candidate,
                    strategy="meta_refresh" if delay > 0 else "html_extract",
                    delay_seconds=float(delay),
                )
                hops.append(hop)
                if progress_callback:
                    progress_callback(hop)
                current_url = next_candidate
                continue

            # No further intermediate links found in HTML; current_url is final
            break

        telemetry_bus.record(
            level="INFO",
            subsystem="provider:shortlink",
            message=f"Shortlink resolution chain completed in {len(hops)} hops: {current_url}",
            context={"start_url": start_url, "final_url": current_url, "hops_count": len(hops)},
        )
        return current_url, hops


    def _extract_embedded_param_url(self, url: str) -> str | None:
        """Extracts nested target URLs inside query string parameters or base64 tokens."""
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)

        for key in ("url", "dest", "destination", "target", "redirect", "r", "u", "link", "go", "to", "v"):
            if key in params:
                val = params[key][0]
                if val.startswith(("http://", "https://")):
                    return val
                # Check base64 encoded URL
                try:
                    padded = val + "=" * (-len(val) % 4)
                    decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
                    if decoded.startswith(("http://", "https://")):
                        return decoded
                except Exception:
                    pass
        return None

    def _detect_captcha(self, html_text: str, page_url: str, task_id: str | None) -> CaptchaChallenge | None:
        """Inspects HTML for Turnstile, reCAPTCHA, or hCaptcha widgets."""
        # Cloudflare Turnstile / Bot Challenge Page
        if (
            "cf-turnstile" in html_text
            or "challenges.cloudflare.com" in html_text
            or "Just a moment..." in html_text
            or "cf-browser-verification" in html_text
        ):
            m = re.search(r'data-sitekey=["\']([^"\']+)["\']', html_text)
            site_key = m.group(1) if m else "cloudflare_managed"
            return CaptchaChallenge(
                task_id=task_id,
                provider_id=urllib.parse.urlsplit(page_url).hostname or "cloudflare",
                captcha_type=CaptchaType.TURNSTILE,
                params={"site_key": site_key, "page_url": page_url},
            )

        # Google reCAPTCHA
        if "g-recaptcha" in html_text or "google.com/recaptcha" in html_text:
            m = re.search(r'data-sitekey=["\']([^"\']+)["\']', html_text)
            if m:
                return CaptchaChallenge(
                    task_id=task_id,
                    provider_id=urllib.parse.urlsplit(page_url).hostname or "google",
                    captcha_type=CaptchaType.RECAPTCHA_V2,
                    params={"site_key": m.group(1), "page_url": page_url},
                )

        # hCaptcha
        if "h-captcha" in html_text or "hcaptcha.com" in html_text:
            m = re.search(r'data-sitekey=["\']([^"\']+)["\']', html_text)
            if m:
                return CaptchaChallenge(
                    task_id=task_id,
                    provider_id=urllib.parse.urlsplit(page_url).hostname or "hcaptcha",
                    captcha_type=CaptchaType.HCAPTCHA,
                    params={"site_key": m.group(1), "page_url": page_url},
                )

        return None

    async def _submit_captcha_token(self, page_url: str, html_text: str, token: str) -> str:
        """Posts completed CAPTCHA token to form action on page."""
        form_parser = _FormExtractor()
        form_parser.feed(html_text)

        if form_parser.forms:
            form = form_parser.forms[0]
            action = urllib.parse.urljoin(page_url, form.get("action", ""))
            data = dict(form.get("inputs", {}))
            # Standard token fields
            data["cf-turnstile-response"] = token
            data["g-recaptcha-response"] = token
            data["h-captcha-response"] = token

            encoded = urllib.parse.urlencode(data).encode("utf-8")
            req = urllib.request.Request(
                action,
                data=encoded,
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": self.user_agent, "Referer": page_url},
            )
            with route_http.urlopen(req, timeout=self.request_timeout) as resp:
                from .challenge_classifier import observe_received_response
                observe_received_response(
                    source="shortlink.token_post", status=getattr(resp, "status", 200),
                    headers=dict(resp.headers), final_url=resp.geturl() or action,
                )
                return resp.geturl()

        return page_url

    def _choose_best_target(self, candidate_links: list[str], current_url: str) -> str | None:
        """Picks the candidate link most likely to be the destination file host."""
        curr_host = urllib.parse.urlsplit(current_url).hostname or ""

        # 1. Prefer known storage hosts
        for link in candidate_links:
            host = urllib.parse.urlsplit(link).hostname or ""
            if any(known in host.lower() for known in _KNOWN_STORAGE_HOSTS):
                return link

        # 2. Prefer direct file extensions
        for link in candidate_links:
            path = urllib.parse.urlsplit(link).path.lower()
            if any(path.endswith(ext) for ext in _DIRECT_FILE_EXTENSIONS):
                return link

        # 3. Prefer external host different from intermediate host
        for link in candidate_links:
            host = urllib.parse.urlsplit(link).hostname or ""
            if host and host.lower() != curr_host.lower():
                return link

        return candidate_links[0] if candidate_links else None

    def _is_terminal_target(self, url: str) -> bool:
        """Checks if URL is already a terminal file hosting site or direct file."""
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        if host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
            return True
        if any(known in host for known in _KNOWN_STORAGE_HOSTS):
            return True
        if host.startswith(("dl.", "cdn.", "storage.", "direct.", "downloads.")):
            return True
        path = parsed.path.lower()
        if any(path.endswith(ext) for ext in _DIRECT_FILE_EXTENSIONS):
            return True
        query = parsed.query.lower()
        if any(ext in query for ext in _DIRECT_FILE_EXTENSIONS):
            return True
        return False

    def _normalize_key(self, url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        return f"{parsed.netloc.lower()}{parsed.path}"

    async def _fetch(self, url: str, method: str = "GET", post_data: dict[str, str] | None = None) -> tuple[int, str, dict[str, str], str, str]:
        if self.http_fetcher:
            res = self.http_fetcher(url)
            if len(res) == 4:
                return res[0], res[1], res[2], res[3], url
            return res

        def _do_fetch() -> tuple[int, str, dict[str, str], str, str]:
            headers = {"User-Agent": self.user_agent}
            encoded_data = None
            if method.upper() == "POST" and post_data is not None:
                encoded_data = urllib.parse.urlencode(post_data).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme and parsed.netloc:
                    headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
                    headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"

            from . import http_client
            try:
                resp = http_client.request(
                    method=method.upper(),
                    url=url,
                    data=post_data,
                    headers=headers,
                    timeout=self.request_timeout,
                    stream=True,
                )
                resp_headers = {k: v for k, v in resp.headers.items()}
                content_type = resp_headers.get("Content-Type", "").lower()
                cd = resp_headers.get("Content-Disposition", "").lower()

                # If server returned an attachment or a binary stream, this is a DIRECT FILE,
                # NOT an HTML shortener redirect page! Close stream immediately and return.
                is_binary = (
                    "attachment" in cd
                    or any(t in content_type for t in ("application/octet-stream", "application/x-", "application/zip", "video/", "audio/"))
                )
                if is_binary:
                    if hasattr(resp, "close"):
                        resp.close()
                    return resp.status, content_type, resp_headers, "", resp.geturl()

                data = resp.read(256 * 1024).decode("utf-8", errors="ignore")
                if hasattr(resp, "close"):
                    resp.close()
                return resp.status, content_type, resp_headers, data, resp.geturl()
            except Exception as http_err:
                status = getattr(http_err, "code", getattr(http_err, "status", 500))
                return status, "", {}, "", url

        return await asyncio.to_thread(_do_fetch)

    def _extract_post_response_target(self, html_text: str, page_url: str) -> str | None:
        """Extracts destination URL or hidden host tokens from dynamic form/post responses."""
        if not html_text:
            return None

        # Look for input with hidden host or destination token
        for m in re.finditer(r'<input[^>]*name=["\'](?:host|url|destination|link|target)["\'][^>]*value=["\']([^"\']+)["\']', html_text, re.I):
            token = html.unescape(m.group(1).strip())
            decoded = self.param_decoder._try_decode_token(token)
            if decoded and is_safe_external_url(decoded):
                return decoded

        for m in re.finditer(r'<input[^>]*value=["\']([^"\']+)["\'][^>]*name=["\'](?:host|url|destination|link|target)["\']', html_text, re.I):
            token = html.unescape(m.group(1).strip())
            decoded = self.param_decoder._try_decode_token(token)
            if decoded and is_safe_external_url(decoded):
                return decoded

        form_parser = _FormExtractor()
        form_parser.feed(html_text)
        for form in form_parser.forms:
            inputs = form.get("inputs", {})
            for key in ("host", "url", "dest", "target", "link"):
                if key in inputs:
                    decoded = self.param_decoder._try_decode_token(html.unescape(str(inputs[key])))
                    if decoded and is_safe_external_url(decoded):
                        return decoded

        return None

