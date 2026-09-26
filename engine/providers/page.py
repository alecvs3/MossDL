from __future__ import annotations

import html
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from ..models import ResolvedItem
from .. import route_http


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        href = values.get("href") or values.get("data-download-url")
        if href and tag.lower() in {"a", "area", "source", "video", "audio"}:
            self.links.append(html.unescape(href))


class PageProvider:
    id = "page"

    @classmethod
    def extract_links(cls, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        headers = {"User-Agent": "transfer-manager/0.1", "Accept": "text/html,application/xhtml+xml"}
        if secrets and secrets.get("referer"):
            headers["Referer"] = secrets["referer"]
        request = urllib.request.Request(url, headers=headers)
        with route_http.urlopen(request, timeout=30) as response:
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError("HTML page exceeds the extraction size limit")
            parser = _LinkParser()
            parser.feed(body.decode(response.headers.get_content_charset() or "utf-8", errors="replace"))
        result: list[ResolvedItem] = []
        seen: set[str] = set()
        source_host = urllib.parse.urlsplit(url).hostname
        allowed_hosts = set((secrets or {}).get("allowed_hosts", "").split(",")) - {""}
        for raw in parser.links:
            link = urllib.parse.urljoin(url, raw)
            parsed = urllib.parse.urlsplit(link)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            if allowed_hosts and parsed.hostname not in allowed_hosts:
                continue
            if not allowed_hosts and parsed.hostname != source_host:
                continue
            canonical = urllib.parse.urldefrag(link).url
            if canonical in seen or len(result) >= 128:
                continue
            seen.add(canonical)
            name = urllib.parse.unquote(parsed.path.rstrip("/").split("/")[-1]) or "download"
            result.append(ResolvedItem(cls.id, url, name, direct_url=canonical,
                                       headers={"User-Agent": "transfer-manager/0.1", "Referer": url},
                                       metadata={"extracted_from": url}))
        return result
