from __future__ import annotations

import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

from ..models import ResolvedItem
from .. import route_http


def _filename(url: str, headers: Message | None = None) -> str:
    if headers:
        value = headers.get_filename()
        if value:
            return os.path.basename(value)
        disposition = headers.get("Content-Disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8'')?['\"]?([^;\"']+)", disposition, re.I)
        if match:
            return os.path.basename(urllib.parse.unquote(match.group(1)))
    name = os.path.basename(urllib.parse.urlsplit(url).path.rstrip("/"))
    return name or "download"


class GenericProvider:
    id = "generic"

    @classmethod
    def manifest(cls) -> dict:
        return {"id": cls.id, "version": "1.0.0", "hosts": ["*"], "capabilities": ["http", "ftp"]}

    @classmethod
    def match(cls, url: str) -> bool:
        return urllib.parse.urlsplit(url).scheme.lower() in {"http", "https", "ftp"}

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        secrets = secrets or {}
        user_agent = secrets.get("user_agent") or "transfer-manager/0.1"
        headers = {"User-Agent": user_agent}
        if secrets.get("referer"):
            headers["Referer"] = secrets["referer"]
        cookies = secrets.get("cookies", {})
        if isinstance(cookies, str):
            cookies = {part.split("=", 1)[0].strip(): part.split("=", 1)[1].strip()
                       for part in cookies.split(";") if "=" in part}
        request = urllib.request.Request(url, method="HEAD", headers=headers)
        response = None
        try:
            response = route_http.urlopen(request, timeout=10)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            response = None
        if response is None and parsed.scheme.lower() in {"http", "https"}:
            fallback_headers = {
                **headers,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                if user_agent == "transfer-manager/0.1" else user_agent
            }
            try:
                request = urllib.request.Request(url, method="GET", headers={**fallback_headers, "Range": "bytes=0-0"})
                response = route_http.urlopen(request, timeout=15)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
                try:
                    request = urllib.request.Request(url, method="GET", headers=fallback_headers)
                    response = route_http.urlopen(request, timeout=15)
                except Exception:
                    response = None
        if response is None:
            name = _filename(url)
            return [ResolvedItem(cls.id, url, name, size=None, direct_url=url)]
        try:
            content_type = response.headers.get("Content-Type", "")
            final_url = response.geturl() or url
            length = response.headers.get("Content-Length")
            size = int(length) if length and length.isdigit() else None
            name = _filename(url, response.headers)
            if name == "download":
                name = _filename(final_url, response.headers)
            if content_type.startswith("text/html") and name == "download":
                name = "download.html"
            return [ResolvedItem(cls.id, url, name, size=size, direct_url=final_url, headers=headers, cookies=cookies,
                                 metadata={"content_type": content_type,
                                           "final_url": final_url,
                                           "redirected": final_url != url,
                                           "status": getattr(response, "status", None)})]
        finally:
            response.close()
