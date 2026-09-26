"""Bounded static page/resource discovery for unknown sites."""

from __future__ import annotations

import html
import json
import mimetypes
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable

from .capture import rank_candidates
from .cms_signatures import CMSRecipeEngine
from .media_extractor import MediaExtractor
from .models import CaptureCandidate
from .telemetry import telemetry_bus
from . import route_http



class _ResourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.resources: list[dict[str, Any]] = []
        self.meta: dict[str, str] = {}
        self._tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        low = tag.lower()
        if low == "meta" and values.get("content"):
            key = values.get("property") or values.get("name")
            if key:
                self.meta[key.lower()] = values["content"]
        keys = ("href", "src", "data-src", "data-url", "data-download-url", "poster")
        for key in keys:
            if values.get(key):
                self.resources.append({"url": html.unescape(values[key]), "tag": low,
                                      "filename": values.get("download")})
        self._tag = low if low == "script" else None

    def handle_data(self, data: str) -> None:
        if self._tag == "script":
            # Keep only URL-shaped strings; never execute page code.
            for match in re.findall(r"https?://[^\"'\s<>]+", data):
                self.resources.append({"url": html.unescape(match), "tag": "script"})

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._tag = None


def _candidate(raw: str, page_url: str, source: str, tag: str = "") -> dict[str, Any] | None:
    url = urllib.parse.urljoin(page_url, html.unescape(raw)).strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https", "ftp"} or not parsed.hostname:
        return None
    mime = mimetypes.guess_type(parsed.path)[0]
    lower = parsed.path.lower()
    confidence = 0.15
    if tag in {"video", "audio", "source"}:
        confidence += 0.30
    if any(lower.endswith(ext) for ext in (".mp4", ".webm", ".mkv", ".mp3", ".m4a", ".jpg", ".png", ".zip", ".pdf")):
        confidence += 0.25
    if mime:
        confidence += 0.15
    return {"url": url, "mime": mime, "confidence": confidence, "source": source, "filename": parsed.path.rsplit("/", 1)[-1]}


class UniversalResolver:
    def __init__(self, max_bytes: int = 8 * 1024 * 1024, max_candidates: int = 128,
                 fetcher: Callable[[str], tuple[str, str]] | None = None,
                 media_extractor: MediaExtractor | None = None,
                 cms_engine: CMSRecipeEngine | None = None) -> None:
        self.max_bytes = max_bytes
        self.max_candidates = max_candidates
        self.fetcher = fetcher
        self.media_extractor = media_extractor or MediaExtractor()
        self.cms_engine = cms_engine or CMSRecipeEngine()

    def inspect(self, url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        telemetry_bus.record(
            level="INFO",
            subsystem="provider:universal",
            message=f"UniversalResolver inspecting {url}",
            context={"url": url},
        )
        # Tier 1: If yt-dlp media extractor can handle or extract media streams, prioritize high-quality streams
        if self.media_extractor and self.media_extractor.can_handle(url):
            media_res = self.media_extractor.extract(url)
            if media_res and media_res.get("selected_url"):
                item = media_res["resolved_item"]
                candidates = [{
                    "url": media_res["selected_url"],
                    "mime": "video/mp4",
                    "confidence": 0.95,
                    "source": "media_extractor",
                    "filename": item.display_name,
                    "thumbnail": media_res.get("thumbnail"),
                    "duration": media_res.get("duration"),
                    "formats": media_res.get("formats", []),
                }]
                telemetry_bus.record(
                    level="INFO",
                    subsystem="provider:universal",
                    message=f"Resolved via media extractor: {item.display_name}",
                    context={"url": url, "selected_url": media_res["selected_url"]},
                )
                return {
                    "source_url": url,
                    "final_url": media_res["selected_url"],
                    "content_type": "video/mp4",
                    "title": media_res.get("title"),
                    "thumbnail": media_res.get("thumbnail"),
                    "candidates": candidates,
                    "resolved_item": item.to_dict(),
                }

        if self.fetcher:
            content_type, text = self.fetcher(url)
            final_url = url
        else:
            request = urllib.request.Request(url, headers=headers or {"User-Agent": "transfer-manager/1"})
            with route_http.urlopen(request, timeout=20) as response:
                final_url = response.geturl() or url
                content_type = response.headers.get("Content-Type", "")
                body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise ValueError("page exceeds the resolver size limit")
                text = body.decode(response.headers.get_content_charset() or "utf-8", errors="replace")

        # Tier 2: Check for cyberlocker CMS patterns (XFileSharing, YetiShare, etc.)
        if self.cms_engine:
            cms_id = self.cms_engine.detect(text)
            if cms_id:
                cms_items = self.cms_engine.resolve(cms_id, final_url, page_html=text)
                if cms_items and cms_items[0].get("direct_url"):
                    first = cms_items[0]
                    cand = {
                        "url": first["direct_url"],
                        "mime": "application/octet-stream",
                        "confidence": 0.90,
                        "source": f"cms_{cms_id}",
                        "filename": first.get("display_name") or "download",
                    }
                    telemetry_bus.record(
                        level="INFO",
                        subsystem="provider:universal",
                        message=f"Resolved via CMS recipe: {cms_id}",
                        context={"url": url, "cms_id": cms_id, "direct_url": first["direct_url"]},
                    )
                    return {
                        "source_url": url,
                        "final_url": first["direct_url"],
                        "content_type": "application/octet-stream",
                        "candidates": [cand],
                        "resolved_item": first,
                        "cms": cms_id,
                    }

        parser = _ResourceParser()
        parser.feed(text)
        values: list[dict[str, Any]] = []
        for key in ("og:video", "og:video:url", "og:audio", "og:image", "twitter:player:stream"):
            if parser.meta.get(key):
                item = _candidate(parser.meta[key], final_url, "opengraph")
                if item:
                    item["confidence"] = min(1.0, float(item["confidence"]) + 0.25)
                    values.append(item)
        values.extend(item for resource in parser.resources
                      if (item := _candidate(resource["url"], final_url, resource.get("tag", ""))))
        for match in re.findall(r"(?:download|url|src|file)\s*[:=]\s*[\"']([^\"']+)", text, re.I):
            item = _candidate(match, final_url, "embedded_json")
            if item:
                item["confidence"] = min(1.0, float(item["confidence"]) + 0.15)
                values.append(item)
        ranked = rank_candidates(values, page_url=final_url, limit=self.max_candidates)
        telemetry_bus.record(
            level="INFO",
            subsystem="provider:universal",
            message=f"Discovered {len(ranked)} candidates for {url}",
            context={"url": url, "count": len(ranked)},
        )
        return {"source_url": url, "final_url": final_url, "content_type": content_type,
                "candidates": [item.to_dict() for item in ranked]}

