from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit
from urllib.request import Request
from typing import Any

from .. import route_http
from ..errors import NeedsUser
from ..media_pipeline import parse_media


class MediaProvider:
    """Parser for non-encrypted HLS/DASH manifests; segment assembly stays engine-owned."""

    def match(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        return path.endswith((".m3u8", ".mpd"))

    def metadata(self, params: dict[str, Any]) -> dict[str, Any]:
        url = params["url"]
        request = Request(url, headers={"User-Agent": "transfer-manager/1"})
        with route_http.urlopen(request, timeout=10) as response:
            body = response.read(8 * 1024 * 1024).decode("utf-8", errors="replace")
        return self.parse(url, body)

    def parse(self, url: str, body: str) -> dict[str, Any]:
        # The pipeline parser carries initialization segments, byte ranges,
        # variant attributes, and timeline information. Keep the old public
        # shape while exposing the richer plan fields to the service.
        result = parse_media(url, body).to_dict()
        result["url"] = url  # compatibility with the original metadata API
        return result

    def _hls(self, url: str, body: str) -> dict[str, Any]:
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if any("#EXT-X-KEY:" in line and "METHOD=NONE" not in line for line in lines):
            raise NeedsUser("encrypted HLS manifests require independent cryptographic review", "unsupported_encryption")
        variants = [urljoin(url, lines[i + 1]) for i, line in enumerate(lines[:-1])
                    if line.startswith("#EXT-X-STREAM-INF") and not lines[i + 1].startswith("#")]
        segments = [urljoin(url, line) for line in lines if not line.startswith("#")]
        return {"kind": "hls", "url": url, "variants": variants, "encrypted": False,
                "segments": [{"index": i, "url": segment} for i, segment in enumerate(segments)]}

    def _dash(self, url: str, body: str) -> dict[str, Any]:
        root = ET.fromstring(body)
        namespaces = {"m": root.tag.split("}", 1)[0][1:]} if "}" in root.tag else {}
        encrypted = any("ContentProtection" in child.tag for child in root.iter())
        if encrypted:
            raise NeedsUser("encrypted DASH manifests require independent cryptographic review", "unsupported_encryption")
        segments: list[dict[str, Any]] = []
        for template in root.iterfind(".//m:SegmentTemplate" if namespaces else ".//SegmentTemplate", namespaces):
            media = template.attrib.get("media")
            initialization = template.attrib.get("initialization")
            base = next((element.text for element in root.iter() if element.tag.endswith("BaseURL") and element.text), "")
            if initialization:
                segments.append({"index": len(segments), "url": urljoin(url, posixpath.join(base, initialization))})
            if media:
                for number in range(1, 10001):
                    candidate = media.replace("$Number$", str(number))
                    if number > 1 and candidate == media:
                        break
                    segments.append({"index": len(segments), "url": urljoin(url, posixpath.join(base, candidate))})
        return {"kind": "dash", "url": url, "variants": [], "encrypted": False, "segments": segments}
