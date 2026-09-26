"""Work out what a pasted link is before anything is downloaded.

One entry point, :func:`analyze_link`, answers "what should the Add window
show for this URL?":

* ``hoster``   a file-host link a dedicated provider understands (single file)
* ``folder``   a file-host folder that can be browsed and picked from
* ``file``     a plain URL that serves a file directly
* ``page``     a web page; its download links are extracted and grouped
* ``shortlink`` a link shortener that could not be unwrapped right now
* ``unsupported`` something the engine cannot download (e.g. magnet links)
* ``unknown``  the link could not be checked; it can still be added as-is

Network access and provider lookups are injected so the logic is testable.
"""

from __future__ import annotations

import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from typing import Any, Callable
from . import route_http

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_PAGE_TYPES = {"text/html", "application/xhtml+xml"}
# Page elements worth annotating with a provider; the rest are navigation.
_DOWNLOAD_CATEGORIES = {"high_utility", "candidate"}


def _filename_from_headers(headers: Message, url: str) -> str | None:
    disposition = headers.get("Content-Disposition") or ""
    match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.I) or re.search(
        r'filename="?([^";]+)"?', disposition, re.I)
    if match:
        return urllib.parse.unquote(match.group(1).strip().strip('"')) or None
    leaf = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    return leaf if "." in leaf else None


def probe_url(url: str, timeout: float = 12.0) -> dict[str, Any]:
    """Fetch response headers only, to tell a file from a web page."""
    request = urllib.request.Request(url, headers={
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Range": "bytes=0-0",
    })
    try:
        response = route_http.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # Error pages (403 from a bot wall, 404) still say what they are.
        response = exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.gaierror):
            return {"ok": False, "error": "the site's address couldn't be found"}
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return {"ok": False, "error": "the site took too long to respond"}
        return {"ok": False, "error": str(reason)}
    try:
        headers = response.headers
        final_url = response.geturl() or url
        content_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        size = None
        content_range = headers.get("Content-Range") or ""
        if "/" in content_range and content_range.rsplit("/", 1)[1].isdigit():
            size = int(content_range.rsplit("/", 1)[1])
        elif (headers.get("Content-Length") or "").isdigit() and getattr(response, "status", None) != 206:
            size = int(headers["Content-Length"])
        attachment = "attachment" in (headers.get("Content-Disposition") or "").lower()
        return {
            "ok": True,
            "status": getattr(response, "status", None) or getattr(response, "code", None),
            "final_url": final_url,
            "content_type": content_type,
            "size": size,
            "filename": _filename_from_headers(headers, final_url),
            "is_page": content_type in _PAGE_TYPES and not attachment,
        }
    finally:
        response.close()


def _page_filename(title: str, url: str) -> str:
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", title or "").strip(" .")
    if not base:
        base = urllib.parse.urlsplit(url).hostname or "page"
    return f"{base[:120].rstrip(' .')}.html"


def analyze_link(
    url: str,
    *,
    inspect: Callable[[str], dict[str, Any]],
    provider_for: Callable[[str], str],
    is_shortlink: Callable[[str], bool],
    unwrap_shortlink: Callable[[str], tuple[str, list[dict[str, Any]]]],
    probe: Callable[[str], dict[str, Any]],
    crawl: Callable[[str], dict[str, Any]],
    _depth: int = 0,
) -> dict[str, Any]:
    url = url.strip()
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme == "magnet":
        return {"url": url, "kind": "unsupported",
                "message": "Magnet and torrent links aren't supported. Paste a direct or file-host link instead."}
    if scheme not in {"http", "https"}:
        return {"url": url, "kind": "unsupported", "message": "Only http and https links can be downloaded."}

    info = inspect(url)
    provider_id = str(info.get("provider_id") or "generic")
    base = {"url": url, "provider_id": provider_id, "provider_name": info.get("display_name") or provider_id}
    if provider_id != "generic":
        # A dedicated provider claimed the link; it knows how to resolve it.
        return {**base, "kind": "folder" if info.get("ui_mode") == "tree_picker" else "hoster"}

    if is_shortlink(url) and _depth == 0:
        try:
            final_url, hops = unwrap_shortlink(url)
        except Exception as exc:  # the engine will retry during the download
            return {**base, "kind": "shortlink",
                    "message": f"Couldn't unwrap this shortlink right now ({exc}). It will be unwrapped when the download starts."}
        if final_url and final_url != url:
            result = analyze_link(final_url, inspect=inspect, provider_for=provider_for,
                                  is_shortlink=is_shortlink, unwrap_shortlink=unwrap_shortlink,
                                  probe=probe, crawl=crawl, _depth=_depth + 1)
            return {**result, "via_shortlink": {"url": url, "hops": len(hops)}}
        return {**base, "kind": "shortlink",
                "message": "This shortlink didn't lead anywhere yet. It will be unwrapped when the download starts."}

    probed = probe(url)
    if not probed.get("ok"):
        return {**base, "kind": "unknown", "message": f"Couldn't reach this link: {probed.get('error') or 'no response'}"}

    final_url = probed.get("final_url") or url
    if final_url != url and _depth < 2:
        # A redirect may land on a file host the engine has a provider for.
        landed = inspect(final_url)
        if (landed.get("provider_id") or "generic") != "generic":
            return {"url": final_url, "provider_id": landed["provider_id"],
                    "provider_name": landed.get("display_name") or landed["provider_id"],
                    "kind": "folder" if landed.get("ui_mode") == "tree_picker" else "hoster",
                    "redirected_from": url}

    if not probed.get("is_page"):
        return {**base, "kind": "file", "file": {
            "name": probed.get("filename"),
            "size": probed.get("size"),
            "mime": probed.get("content_type") or None,
        }}

    try:
        page = crawl(final_url)
    except Exception as exc:
        return {**base, "kind": "page", "page": {
            "url": final_url, "title": urllib.parse.urlsplit(final_url).hostname or final_url,
            "elements": [], "total_elements": 0, "error": str(exc),
            "save_as": _page_filename("", final_url),
        }}
    elements = []
    for element in page.get("elements", []):
        element = dict(element)
        target = str(element.get("target_url") or "")
        if element.get("category") in _DOWNLOAD_CATEGORIES and target.startswith(("http://", "https://")):
            try:
                element["provider_id"] = provider_for(target)
            except Exception:
                element["provider_id"] = None
        if target.lower().startswith("magnet:"):
            element["unsupported"] = True
        elements.append(element)
    return {**base, "kind": "page", "page": {
        "url": final_url,
        "title": page.get("title") or final_url,
        "favicon": page.get("favicon"),
        "total_elements": page.get("total_elements", len(elements)),
        "ads_stripped": page.get("ads_stripped", 0),
        "used_headless": page.get("used_headless", False),
        "has_countdown_timer": page.get("has_countdown_timer", False),
        "elements": elements,
        "save_as": _page_filename(page.get("title") or "", final_url),
    }}
