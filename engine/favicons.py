"""Per-host favicon cache.

A site's icon is fetched once, from the site itself (its declared
``<link rel=icon>`` when a crawl saw one, else ``/favicon.ico``), and kept on
disk. Misses are remembered too, so an icon-less host is not re-fetched on
every render. No third-party favicon services are contacted.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .telemetry import telemetry_bus

MAX_BYTES = 256 * 1024
MISS_TTL_SECONDS = 3 * 24 * 3600
FETCH_TIMEOUT = 5.0
_HOST_RE = re.compile(r"^[a-z0-9.-]+$")
# Magic bytes → MIME, so a login page served as /favicon.ico is not cached as an icon.
_SIGNATURES = (
    (b"\x00\x00\x01\x00", "image/x-icon"),
    (b"\x89PNG", "image/png"),
    (b"GIF8", "image/gif"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),
)


def normalize_host(value: str) -> str | None:
    """Host from a host name or URL, lowercased, without ``www.``."""
    value = (value or "").strip().lower()
    if "://" in value:
        value = urlsplit(value).hostname or ""
    value = value.split("/")[0].split(":")[0]
    if value.startswith("www."):
        value = value[4:]
    return value if value and _HOST_RE.match(value) and "." in value else None


def _sniff(data: bytes, declared: str | None) -> str | None:
    for magic, mime in _SIGNATURES:
        if data.startswith(magic):
            return mime
    head = data[:256].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:1024].lower()):
        return "image/svg+xml"
    if declared and declared.startswith("image/") and not head.startswith(b"<"):
        return declared
    return None


class FaviconCache:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._hints: dict[str, str] = {}
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="favicon")

    def remember_hint(self, page_url: str, icon_url: str | None) -> None:
        """Called when a crawl sees a page's declared icon."""
        host = normalize_host(page_url)
        if host and icon_url and icon_url.startswith(("http://", "https://")):
            with self._lock:
                self._hints.setdefault(host, icon_url)

    def _paths(self, host: str) -> tuple[Path, Path]:
        return self.directory / f"{host}.bin", self.directory / f"{host}.json"

    def _cached(self, host: str) -> dict[str, Any] | None:
        data_path, meta_path = self._paths(host)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if meta.get("mime") and data_path.exists():
            return {"mime": meta["mime"], "data": data_path.read_bytes()}
        if time.time() - float(meta.get("checked_at", 0)) < MISS_TTL_SECONDS:
            return {"mime": None, "data": b""}
        return None

    def _candidates(self, host: str) -> list[str]:
        urls = []
        with self._lock:
            hint = self._hints.get(host)
        if hint:
            urls.append(hint)
        labels = host.split(".")
        # sub.example.co → example.co: CDNs and mirrors rarely carry their own icon.
        for i in range(len(labels) - 1):
            urls.append(f"https://{'.'.join(labels[i:])}/favicon.ico")
            if len(labels) - i <= 2:
                break
        return urls

    def _fetch(self, host: str) -> dict[str, Any]:
        from . import http_client

        tried: list[str] = []
        for url in self._candidates(host):
            try:
                resp = http_client.request("GET", url, timeout=FETCH_TIMEOUT, headers={"Accept": "image/*,*/*;q=0.5"})
                data = resp.read(MAX_BYTES + 1) if resp.status < 400 else b""
                declared = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower() or None
            except Exception as exc:  # network errors are expected here; logged and the next candidate tried
                tried.append(f"{url}: {type(exc).__name__}")
                continue
            mime = _sniff(data, declared) if data and len(data) <= MAX_BYTES else None
            if mime:
                return {"mime": mime, "data": data}
            tried.append(f"{url}: {'too large' if len(data) > MAX_BYTES else 'not an image'} (HTTP {resp.status})")
        telemetry_bus.record(level="DEBUG", subsystem="engine:favicons", tier="engine",
                             message=f"no favicon for {host}", context={"host": host, "tried": tried})
        return {"mime": None, "data": b""}

    def _store(self, host: str, result: dict[str, Any]) -> None:
        data_path, meta_path = self._paths(host)
        try:
            if result["mime"]:
                data_path.write_bytes(result["data"])
            meta_path.write_text(json.dumps({"mime": result["mime"], "checked_at": time.time()}), encoding="utf-8")
        except OSError as exc:
            telemetry_bus.record(level="WARNING", subsystem="engine:favicons", tier="engine",
                                 message="could not write favicon cache", context={"host": host, "error": str(exc)})

    def _resolve(self, host: str) -> dict[str, Any]:
        cached = self._cached(host)
        if cached is not None:
            return cached
        with self._lock:
            waiter = self._inflight.get(host)
            owner = waiter is None
            if owner:
                waiter = self._inflight[host] = threading.Event()
        if not owner:
            waiter.wait(FETCH_TIMEOUT * 3)
            return self._cached(host) or {"mime": None, "data": b""}
        try:
            result = self._fetch(host)
            self._store(host, result)
            return result
        finally:
            with self._lock:
                self._inflight.pop(host, None)
            waiter.set()

    def get_many(self, hosts: list[str]) -> dict[str, str | None]:
        """Returns ``{host: data URL or None}`` for each requested host."""
        wanted = {h: normalize_host(h) for h in hosts[:64]}
        unique = sorted({h for h in wanted.values() if h})
        results = dict(zip(unique, self._pool.map(self._resolve, unique)))
        out: dict[str, str | None] = {}
        for original, host in wanted.items():
            r = results.get(host) if host else None
            out[original] = f"data:{r['mime']};base64,{base64.b64encode(r['data']).decode()}" if r and r["mime"] else None
        return out
