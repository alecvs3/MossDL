from __future__ import annotations

import base64
import html
import ipaddress
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from . import route_http


_URL_FIELDS = {"url", "target", "dest", "destination", "redirect", "redirect_url", "u", "r", "link", "go"}
_PROVIDER_HOSTS = {"gofile.io", "api.gofile.io", "mega.nz", "transfer.it", "g.api.mega.co.nz"}
_NAVIGATION_TAGS = {"a", "area", "form"}
_URL_ATTRIBUTES = {"href", "action", "data-url", "data-link", "data-target", "data-redirect",
                   "data-download-url"}
_SCRIPT_URL_RE = re.compile(
    r"(?:url|target|dest(?:ination)?|redirect(?:_url)?|download(?:Url|_url)?|link)\s*[:=]\s*['\"]([^'\"]+)['\"]",
    re.I,
)


class _TargetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: list[str] = []
        self._in_script = False
        self._script_data: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag.lower() in _NAVIGATION_TAGS:
            for name in ("href", "action"):
                if attributes.get(name):
                    self.values.append(attributes[name] or "")
        for name in _URL_ATTRIBUTES - {"href", "action"}:
            if attributes.get(name):
                self.values.append(attributes[name] or "")
        if tag.lower() == "meta" and attributes.get("http-equiv", "").lower() == "refresh":
            match = re.search(r"url\s*=\s*(.+)$", attributes.get("content", ""), re.I)
            if match:
                self.values.append(match.group(1).strip().strip("'\""))
        self._in_script = tag.lower() == "script"
        if self._in_script:
            self._script_data = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_data.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_script:
            self.values.extend(match.group(1) for match in _SCRIPT_URL_RE.finditer("".join(self._script_data)))
            self._in_script = False
            self._script_data = []


def load_catalog(path: str | Path | None = None) -> dict[str, Any]:
    catalog_path = Path(path) if path else Path(__file__).resolve().parents[1] / "plugins" / "shortlink" / "catalog.json"
    if not catalog_path.is_file():
        return {"version": 1, "entries": []}
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def catalog_hosts(catalog: dict[str, Any]) -> set[str]:
    return {str(entry["host"]).lower().lstrip("www.") for entry in catalog.get("entries", []) if entry.get("host")}


def is_catalogued(url: str, catalog: dict[str, Any]) -> bool:
    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").lower().lstrip("www.")
    if hostname in _PROVIDER_HOSTS:
        return False
    if hostname == "network-loop.com" and parsed.path == "/.safe/redirect.html" and "u" in parsed.query:
        return True
    return hostname in catalog_hosts(catalog)


def _safe_url(value: str, source_url: str) -> str | None:
    value = html.unescape(urllib.parse.unquote(str(value))).strip().strip("'\"")
    if value.startswith("//"):
        value = f"{urllib.parse.urlsplit(source_url).scheme}:{value}"
    if not value.lower().startswith(("http://", "https://")):
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.username or parsed.password or not parsed.hostname:
        return None
    try:
        address = ipaddress.ip_address(parsed.hostname)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return None
    except ValueError:
        pass
    return value


def _decode_candidate(value: str) -> list[str]:
    candidates = [value]
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        candidates.append(decoded)
    except (ValueError, UnicodeDecodeError):
        pass
    return candidates


def _is_resource_candidate(value: str, source_url: str) -> bool:
    """Reject page resources, web tracking, and ads accidentally found in inline/static HTML."""
    parsed = urllib.parse.urlsplit(value)
    path = parsed.path.lower()
    if path.endswith((".css", ".js", ".mjs", ".map", ".woff", ".woff2", ".ttf", ".ico")):
        return True
    host = (parsed.hostname or "").lower().lstrip("www.")
    if any(ad_domain in host for ad_domain in {
        "adnxs.com", "doubleclick.net", "google-analytics.com", "googletagmanager.com",
        "prebid", "criteo.com", "rubiconproject.com", "pubmatic.com", "openx.net",
        "outbrain.com", "taboola.com", "adroll.com", "advertising.com"
    }):
        return True
    return host in {"unpkg.com", "cdnjs.cloudflare.com", "cdn.jsdelivr.net", "fonts.googleapis.com",
                    "fonts.gstatic.com"} and parsed.hostname != urllib.parse.urlsplit(source_url).hostname


def extract_static_targets(source_url: str, body: str | None = None, limit: int = 16) -> list[str]:
    values: list[str] = []
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(source_url).query, keep_blank_values=False)
    for key, entries in query.items():
        if key.lower() in _URL_FIELDS:
            values.extend(entries)
    if body:
        parser = _TargetParser()
        parser.feed(body)
        values.extend(parser.values)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for candidate in _decode_candidate(value):
            safe = _safe_url(candidate, source_url)
            if safe and not _is_resource_candidate(safe, source_url) and safe not in seen:
                seen.add(safe)
                result.append(safe)
                if len(result) >= limit:
                    return result
    return result


def fetch_and_extract(source_url: str, timeout: float = 10.0, max_bytes: int = 2 * 1024 * 1024) -> list[str]:
    request = urllib.request.Request(source_url, headers={"User-Agent": "transfer-manager-shortlink/1"})
    with route_http.urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("shortlink response exceeds the configured size limit")
    targets = extract_static_targets(source_url, body.decode("utf-8", errors="replace"))
    final_url = response.geturl()
    if final_url != source_url and final_url not in targets:
        targets.insert(0, final_url)
    return targets


def looks_like_html_item(item: Any) -> bool:
    metadata = getattr(item, "metadata", {}) or {}
    content_type = str(metadata.get("content_type", "")).lower()
    name = str(getattr(item, "display_name", "")).lower()
    return content_type.startswith("text/html") or name.endswith((".html", ".htm"))
