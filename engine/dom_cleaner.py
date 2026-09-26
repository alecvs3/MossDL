from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

# Community adblock & deceptive network domain filters (EasyList, uBlock Origin, Peter Lowe)
EASYLIST_AD_DOMAINS = {
    "adsterra.com", "popads.net", "popcash.net", "propellerads.com",
    "exoclick.com", "monetag.com", "hilltopads.com", "adcash.com",
    "clickadu.com", "trafficjunky.net", "juicyads.com", "admaven.com",
    "bidgear.com", "yllix.com", "revenuehits.com", "mgid.com",
    "taboola.com", "outbrain.com", "revcontent.com", "criteo.com",
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adskeeper.co.uk", "adskeeper.com", "adrecover.com", "zergnet.com",
    "onclickprediction.com", "alwingulla.com", "whomeeno.com",
    "shoptonten.club", "highcpmrevenuenetwork.com", "directrev.com",
    "adtrue.com", "ad-maven.com", "clicksor.com", "infolinks.com",
    "adclx.com", "adnxs.com", "rubiconproject.com", "openx.net",
    "casalemedia.com", "pubmatic.com", "bidswitch.net", "smartadserver.com",
    "yieldmo.com", "sovrn.com", "contextweb.com", "exponential.com",
    "adblade.com", "inmobi.com", "applovin.com", "ironsrc.com",
    "unityads.unity3d.com", "startapp.com", "vungle.com", "chartboost.com",
    "leadbolt.com", "airpush.com", "tapjoy.com", "fyber.com",
    # Hoster interstitial / popunder networks observed on datanodes.to
    "dlhaven.com", "dlhaven.net", "linkpoi.me", "doubleverify.com", "adsrvr.org",
}

# Not ads: real media hosts whose embeds a solver browser should not load on a
# hoster's interstitial page (they cost seconds and never lead to the file).
# Crawl results keep these links; only headless page loads skip them.
SOLVER_BLOCKED_EMBED_DOMAINS = {"youtube.com", "youtu.be", "googlevideo.com", "ytimg.com"}

# Cosmetic selector regex patterns matching classes, IDs, or names
_COSMETIC_AD_RE = re.compile(
    r"(?:^|[-_])(ad|ads|adbanner|ad-banner|ad-container|ad-wrapper|ad-box|advertisement|"
    r"sponsored|sponsor|fake-download|fake_download|fakebtn|popads|adsterra|monetag|clickjacking|"
    r"direct-ad|ad-unit|adsbygoogle|banner-ad|native-ad|taboola|outbrain|criteo|"
    r"ad-placement|ad-slot|ad-holder|ad-content|ad-sidebar|promo-banner)(?:$|[-_])",
    re.I,
)

_COUNTDOWN_RE = re.compile(
    r"(?:wait|please wait|ready in|countdown|remaining|timer)\s*(?:is|:)?\s*(\d+)\s*(?:s|sec|seconds)?",
    re.I,
)


def _host_in(url: str, domains: set[str]) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return bool(host) and any(host == domain or host.endswith("." + domain) for domain in domains)


def is_solver_blocked_embed(url: str) -> bool:
    """True for heavy media embeds a headless solver page load should skip."""
    try:
        return bool(url) and _host_in(url, SOLVER_BLOCKED_EMBED_DOMAINS)
    except ValueError:
        return False


def is_ad_domain(url: str) -> bool:
    """Check whether the URL points to a known ad network or tracking redirector."""
    if not url:
        return False
    try:
        return _host_in(url, EASYLIST_AD_DOMAINS)
    except ValueError:  # malformed URL (e.g. bad IPv6 literal): not an ad host
        return False


def is_cosmetic_ad_attribute(val: str | None) -> bool:
    """Check whether a class or id attribute matches cosmetic ad patterns."""
    if not val:
        return False
    for token in str(val).split():
        if _COSMETIC_AD_RE.search(token):
            return True
    return False


def is_clickjacking_overlay(style: str | None) -> bool:
    """Detect transparent or full-viewport interceptor overlays."""
    if not style:
        return False
    s = style.lower().replace(" ", "")
    is_fullscreen = (
        ("width:100%" in s or "width:100vw" in s or "right:0;left:0" in s or "inset:0" in s) and
        ("height:100%" in s or "height:100vh" in s or "bottom:0;top:0" in s)
    )
    is_positioned = "position:fixed" in s or "position:absolute" in s
    has_high_z = bool(re.search(r"z-index\s*:\s*(?:999\d+|[1-9]\d{4,})", style, re.I))
    is_transparent = "opacity:0" in s or "rgba(0,0,0,0)" in s or "transparent" in s

    if is_positioned and (has_high_z or is_fullscreen) and (is_transparent or is_fullscreen):
        return True
    return False


@dataclass
class CleanedElement:
    tag: str  # 'a', 'button', 'form'
    text: str
    target_url: str
    method: str = "GET"
    form_inputs: dict[str, str] = field(default_factory=dict)
    element_id: str = ""
    element_classes: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    is_ad: bool = False
    filter_reason: str = ""
    context_hint: str = ""


class DomCleaner(HTMLParser):
    """
    Extracts interactive links, buttons, and forms from HTML pages while stripping
    cosmetic ad banners, deceptive fake download buttons, clickjacking overlays,
    and ad network URLs using EasyList/uBlock Origin rules and structural heuristics.
    """

    def __init__(self, base_url: str = "") -> None:
        super().__init__()
        self.base_url = base_url
        self.page_title = ""
        self.favicon_url = ""
        self.elements: list[CleanedElement] = []
        self.stripped_ads: list[CleanedElement] = []
        self.has_countdown_timer = False
        self.countdown_seconds: int | None = None
        self.countdown_source: str = ""
        self.countdown_parse_miss: bool = False
        self._line_context = ""

        # Parsing state
        self._in_title = False
        self._title_chunks: list[str] = []
        self._tag_stack: list[dict[str, Any]] = []
        self._ad_depth = 0
        self._countdown_depth = 0

        # Current active interactive element collectors
        self._current_anchor: dict[str, Any] | None = None
        self._current_button: dict[str, Any] | None = None
        self._current_form: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, raw_attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        attrs = {k.lower(): v or "" for k, v in raw_attrs}

        # Check for cosmetic ad classes/IDs or clickjacking styles
        is_ad_element = False
        reason = ""

        classes = attrs.get("class", "").split()
        element_id = attrs.get("id", "")
        style = attrs.get("style", "")

        if is_cosmetic_ad_attribute(attrs.get("class")) or is_cosmetic_ad_attribute(element_id):
            is_ad_element = True
            reason = "cosmetic_selector_match"
        elif is_clickjacking_overlay(style):
            is_ad_element = True
            reason = "clickjacking_overlay"

        if is_ad_element:
            self._ad_depth += 1

        is_countdown_container = bool(
            any("countdown" in c.lower() or "timer" in c.lower() for c in classes)
            or "countdown" in element_id.lower()
        )

        self._tag_stack.append({
            "tag": tag_lower,
            "is_ad": is_ad_element,
            "attrs": attrs,
            "countdown": is_countdown_container,
        })

        if is_countdown_container:
            self.has_countdown_timer = True
            self._countdown_depth += 1
            if self.countdown_seconds is None:
                for attr_name in ("data-seconds", "data-timer", "data-countdown", "data-wait"):
                    raw_val = attrs.get(attr_name, "").strip()
                    if raw_val.isdigit() and int(raw_val) > 0:
                        self.countdown_seconds = int(raw_val)
                        self.countdown_source = f"attribute:{attr_name}"
                        break

        if tag_lower == "title":
            self._in_title = True
            self._title_chunks = []
            return

        if tag_lower == "link":
            rel = (attrs.get("rel") or "").lower()
            if "icon" in rel:
                href = attrs.get("href", "").strip()
                if href:
                    self.favicon_url = urllib.parse.urljoin(self.base_url, href) if self.base_url else href
            return

        if tag_lower in ("br", "p", "tr", "div", "li", "hr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._line_context = ""

        # Form tracking
        if tag_lower == "form":
            raw_action = attrs.get("action", "")
            action_url = urllib.parse.urljoin(self.base_url, raw_action) if self.base_url else raw_action
            self._current_form = {
                "action": action_url,
                "method": attrs.get("method", "GET").upper(),
                "id": element_id,
                "classes": classes,
                "attrs": attrs,
                "inputs": {},
                "text_chunks": [],
                "is_ad": self._ad_depth > 0,
                "reason": reason or ("ad_container_child" if self._ad_depth > 0 else ""),
                "context_hint": self._line_context,
            }
            return

        # Input tracking (hidden tokens, session keys inside forms)
        if tag_lower == "input":
            name = attrs.get("name")
            val = attrs.get("value", "")
            if self._current_form and name:
                self._current_form["inputs"][name] = val
            return

        # Anchor tracking
        if tag_lower == "a":
            raw_href = attrs.get("href", "").strip()
            if not raw_href or raw_href.lower().startswith(("javascript:", "data:", "vbscript:", "#", "void(0)")):
                for candidate_attr in ("data-url", "data-href", "data-download-url", "data-target", "data-link", "data-destination"):
                    val = attrs.get(candidate_attr, "").strip()
                    if val and not val.lower().startswith(("javascript:", "#")):
                        raw_href = val
                        break
            target_url = urllib.parse.urljoin(self.base_url, raw_href) if self.base_url and raw_href else raw_href
            self._current_anchor = {
                "href": target_url,
                "raw_href": raw_href,
                "id": element_id,
                "classes": classes,
                "attrs": attrs,
                "text_chunks": [],
                "is_ad": self._ad_depth > 0 or is_ad_domain(target_url),
                "reason": reason or ("ad_network_domain" if is_ad_domain(target_url) else ("ad_container_child" if self._ad_depth > 0 else "")),
                "context_hint": self._line_context,
            }
            return

        # Button tracking
        if tag_lower == "button":
            formaction = attrs.get("formaction", "")
            data_link_key = attrs.get("data-link-key")
            data_post_id = attrs.get("data-post-id")
            data_shortcode_id = attrs.get("data-shortcode-id")

            form_inputs: dict[str, str] = {}
            method = "GET"
            target_url = ""

            # Safelink / processing button pattern (e.g. Ryuugames safelink buttons)
            if data_link_key and data_post_id:
                target_url = urllib.parse.urljoin(self.base_url, "/processing/") if self.base_url else "/processing/"
                method = "POST"
                form_inputs = {
                    "ryuu_sl_action": "process",
                    "post_id": data_post_id,
                    "shortcode_id": data_shortcode_id or "",
                    "link_key": data_link_key,
                }
            elif formaction:
                target_url = urllib.parse.urljoin(self.base_url, formaction) if self.base_url else formaction
            elif self._current_form:
                target_url = self._current_form["action"]
                method = self._current_form["method"]
            else:
                for candidate_attr in ("data-url", "data-href", "data-download-url", "data-target", "data-link", "data-destination"):
                    val = attrs.get(candidate_attr, "").strip()
                    if val and not val.lower().startswith(("javascript:", "#")):
                        target_url = urllib.parse.urljoin(self.base_url, val) if self.base_url else val
                        break

            self._current_button = {
                "type": attrs.get("type", "button").lower(),
                "target_url": target_url,
                "method": method,
                "form_inputs": form_inputs,
                "id": element_id,
                "classes": classes,
                "attrs": attrs,
                "text_chunks": [],
                "is_ad": self._ad_depth > 0,
                "reason": reason or ("ad_container_child" if self._ad_depth > 0 else ""),
                "context_hint": self._line_context,
            }
            return

        # Image inside anchor or button (capturing alt/title text)
        if tag_lower == "img":
            alt_text = attrs.get("alt") or attrs.get("title") or ""
            if alt_text:
                if self._current_anchor:
                    self._current_anchor["text_chunks"].append(alt_text)
                if self._current_button:
                    self._current_button["text_chunks"].append(alt_text)
                if self._current_form:
                    self._current_form["text_chunks"].append(alt_text)

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return

        if self._in_title:
            self._title_chunks.append(text)
            return

        # Check for countdown timers in text
        match = _COUNTDOWN_RE.search(text)
        if match:
            self.has_countdown_timer = True
            try:
                self.countdown_seconds = int(match.group(1))
                self.countdown_source = self.countdown_source or "text"
            except (ValueError, TypeError):
                pass
        elif self._countdown_depth > 0 and self.countdown_seconds is None:
            # The container itself only holds a bare number (e.g. <div class="countdown">10</div>)
            bare = re.search(r"(\d{1,4})", text)
            if bare:
                self.countdown_seconds = int(bare.group(1))
                self.countdown_source = "container_text"

        if not self._current_anchor and not self._current_button and not self._current_form:
            m = re.search(r'([A-Za-z0-9_\-\.]+\.(?:7z\.\d{3}|part\d+\.rar|\d{3}|rar|zip|7z|tar|gz|iso|bin|exe))', text, re.I)
            if m:
                self._line_context = m.group(1)
            elif not self._line_context:
                self._line_context = text[:120]

        if self._current_anchor:
            self._current_anchor["text_chunks"].append(text)
        if self._current_button:
            self._current_button["text_chunks"].append(text)
        if self._current_form:
            self._current_form["text_chunks"].append(text)

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()

        if tag_lower == "title":
            self._in_title = False
            self.page_title = " ".join(self._title_chunks).strip()
            return

        # Pop from tag stack and adjust ad depth
        if self._tag_stack:
            item = self._tag_stack.pop()
            if item.get("is_ad"):
                self._ad_depth = max(0, self._ad_depth - 1)
            if item.get("countdown"):
                self._countdown_depth = max(0, self._countdown_depth - 1)
                if self._countdown_depth == 0 and self.countdown_seconds is None:
                    self._emit_countdown_parse_miss()

        # Finalize anchor
        if tag_lower == "a" and self._current_anchor:
            data = self._current_anchor
            self._current_anchor = None

            full_text = " ".join(data["text_chunks"]).strip()
            target_url = data["href"]
            raw_href = data["raw_href"]

            # Check if href is javascript:, void, or empty
            is_void = (
                not raw_href or
                raw_href.lower().startswith(("javascript:", "data:", "vbscript:", "#", "void(0)"))
            )

            is_ad = data["is_ad"] or is_ad_domain(target_url)
            reason = data["reason"] or ("ad_network_domain" if is_ad_domain(target_url) else "")

            elem = CleanedElement(
                tag="a",
                text=full_text,
                target_url=target_url,
                method="GET",
                element_id=data["id"],
                element_classes=data["classes"],
                attributes=data["attrs"],
                is_ad=is_ad,
                filter_reason=reason,
                context_hint=data.get("context_hint", ""),
            )

            if is_ad:
                self.stripped_ads.append(elem)
            elif not is_void:
                self.elements.append(elem)
            return

        # Finalize button
        if tag_lower == "button" and self._current_button:
            data = self._current_button
            self._current_button = None

            full_text = " ".join(data["text_chunks"]).strip()
            target_url = data["target_url"]

            is_ad = data["is_ad"] or is_ad_domain(target_url)
            reason = data["reason"] or ("ad_network_domain" if is_ad_domain(target_url) else "")

            # Pull form inputs from button data and form container if present
            form_inputs = dict(data.get("form_inputs", {}))
            method = data.get("method", "GET")
            if self._current_form:
                form_inputs.update(self._current_form["inputs"])
                method = self._current_form["method"]
                if not target_url:
                    target_url = self._current_form["action"]

            elem = CleanedElement(
                tag="button",
                text=full_text,
                target_url=target_url,
                method=method,
                form_inputs=form_inputs,
                element_id=data["id"],
                element_classes=data["classes"],
                attributes=data["attrs"],
                is_ad=is_ad,
                filter_reason=reason,
                context_hint=data.get("context_hint", ""),
            )

            if is_ad:
                self.stripped_ads.append(elem)
            else:
                self.elements.append(elem)
            return

        # Finalize form
        if tag_lower == "form" and self._current_form:
            data = self._current_form
            self._current_form = None

            full_text = " ".join(data["text_chunks"]).strip()
            target_url = data["action"]

            is_ad = data["is_ad"] or is_ad_domain(target_url)
            reason = data["reason"] or ("ad_network_domain" if is_ad_domain(target_url) else "")

            # Only append the standalone form element if we didn't already extract a button inside it
            button_already_extracted = any(
                e.tag == "button" and e.target_url == target_url
                for e in self.elements
            )

            elem = CleanedElement(
                tag="form",
                text=full_text,
                target_url=target_url,
                method=data["method"],
                form_inputs=data["inputs"],
                element_id=data["id"],
                element_classes=data["classes"],
                attributes=data["attrs"],
                is_ad=is_ad,
                filter_reason=reason,
                context_hint=data.get("context_hint", ""),
            )

            if is_ad:
                self.stripped_ads.append(elem)
            elif not button_already_extracted:
                self.elements.append(elem)
            return

    def _emit_countdown_parse_miss(self) -> None:
        """Emits a structured [TIMER_PARSE_MISS] when a countdown yielded no seconds."""
        if self.countdown_parse_miss:
            return
        self.countdown_parse_miss = True
        try:
            from .telemetry import telemetry_bus
            telemetry_bus.record(
                level="WARN",
                subsystem="engine:timer",
                message="[TIMER_PARSE_MISS] countdown element detected but no seconds value parsed",
                context={
                    "url": self.base_url,
                    "title": self.page_title,
                    "countdown_source": self.countdown_source or "none",
                },
                tier="engine",
            )
        except Exception:
            pass

    def close(self) -> None:
        super().close()
        if not self.favicon_url and self.base_url:
            try:
                parsed = urllib.parse.urlsplit(self.base_url)
                if parsed.scheme and parsed.netloc:
                    self.favicon_url = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
            except Exception:
                pass
        if self.has_countdown_timer and self.countdown_seconds is None:
            self._emit_countdown_parse_miss()

