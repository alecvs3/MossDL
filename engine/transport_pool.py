from __future__ import annotations

import logging
import threading
from typing import Any, Iterator
import urllib.parse

import httpx

from .route_http import proxy_url_for

logger = logging.getLogger(__name__)


class PooledTransportManager:
    """
    High-performance pooled HTTP transport client.
    Maintains persistent, keep-alive TCP/TLS connections across multiple concurrent
    download segments, eliminating handshake latency and socket thrashing.
    """

    def __init__(
        self,
        max_keepalive_connections: int = 40,
        max_connections: int = 80,
        keepalive_expiry: float = 60.0,
        default_timeout: float = 60.0,
    ) -> None:
        self.limits = httpx.Limits(
            max_keepalive_connections=max_keepalive_connections,
            max_connections=max_connections,
            keepalive_expiry=keepalive_expiry,
        )
        self.default_timeout = default_timeout
        self._clients: dict[str, httpx.Client] = {}
        self._lock = threading.Lock()

    def _client_key(self, route_profile: dict[str, Any] | None) -> str:
        return proxy_url_for(route_profile) or "direct"

    def get_client(self, route_profile: dict[str, Any] | None = None) -> httpx.Client:
        key = self._client_key(route_profile)
        with self._lock:
            if key not in self._clients:
                # The shared route rules: socks5h keeps DNS on the proxy side.
                proxy_url = None if key == "direct" else key
                client = httpx.Client(
                    proxy=proxy_url,
                    limits=self.limits,
                    timeout=httpx.Timeout(self.default_timeout, connect=15.0),
                    follow_redirects=True,
                    trust_env=False,
                )
                self._clients[key] = client
            return self._clients[key]

    def probe(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        route_profile: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, int | bool | str | None]:
        """
        Probes target URL with HEAD or range 0-0 request to determine content size
        and range support, reusing pooled connections.
        """
        client = self.get_client(route_profile)
        req_headers = dict(headers or {})
        head_headers: dict[str, str] = {}
        head_status: int | None = None
        try:
            head = client.head(url, headers=req_headers, timeout=timeout)
            head_status = head.status_code
            head_headers = dict(head.headers)
            head.close()
        except Exception:
            # A range GET remains the authoritative probe when HEAD is blocked.
            head = None

        range_headers = {**req_headers, "Range": "bytes=0-0"}
        range_response = None
        try:
            request = client.build_request('GET', url, headers=range_headers, timeout=timeout)
            range_response = client.send(request, stream=True)
            response_headers = dict(range_response.headers)
            status = range_response.status_code
            content_range = response_headers.get("content-range", "")
            length = response_headers.get("content-length")
            if status == 206 or status == 416 or status == 200:
                merged_headers = {**head_headers, **response_headers}
                size: int | None = None
                if "/" in content_range:
                    suffix = content_range.rsplit("/", 1)[1]
                    if suffix.isdigit():
                        size = int(suffix)
                if size is None and length and length.isdigit() and status != 206:
                    size = int(length)
                if size is None:
                    head_length = head_headers.get("content-length")
                    if head_length and head_length.isdigit():
                        size = int(head_length)
                valid_probe_range = status == 206 and content_range.lower().startswith("bytes 0-0/")
                return {
                    "size": size,
                    "ranges": valid_probe_range,
                    "status_code": status,
                    "range_requested": True,
                    "content_range": content_range,
                    "etag": merged_headers.get("etag"),
                    "last_modified": merged_headers.get("last-modified"),
                    "validator": merged_headers.get("etag") or merged_headers.get("last-modified"),
                    "headers": merged_headers,
                }
            return {
                "size": None,
                "ranges": False,
                "status_code": status,
                "range_requested": True,
                "content_range": content_range,
                "etag": response_headers.get("etag"),
                "last_modified": response_headers.get("last-modified"),
                "validator": response_headers.get("etag") or response_headers.get("last-modified"),
                "headers": {**head_headers, **response_headers},
            }
        except Exception:
            if head_status is None:
                raise
            head_length = head_headers.get("content-length")
            head_range = head_headers.get("content-range", "")
            size = int(head_length) if head_length and head_length.isdigit() else None
            if "/" in head_range and head_range.rsplit("/", 1)[1].isdigit():
                size = int(head_range.rsplit("/", 1)[1])
            return {
                "size": size,
                "ranges": head_headers.get("accept-ranges", "").lower() == "bytes",
                "status_code": head_status,
                "range_requested": False,
                "content_range": head_range,
                "etag": head_headers.get("etag"),
                "last_modified": head_headers.get("last-modified"),
                "validator": head_headers.get("etag") or head_headers.get("last-modified"),
                "headers": head_headers,
            }
        finally:
            if range_response is not None:
                range_response.close()

    def open_range_stream(
        self,
        url: str,
        start: int,
        end: int,
        headers: dict[str, str] | None = None,
        route_profile: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> tuple[httpx.Response, Iterator[bytes]]:
        """
        Opens a pooled HTTP range stream [start..end] and yields bytes.
        """
        client = self.get_client(route_profile)
        req_headers = dict(headers or {})
        req_headers["Range"] = f"bytes={start}-{end}"
        
        req = client.build_request("GET", url, headers=req_headers)
        resp = client.send(req, stream=True)
        if resp.status_code not in {200, 206}:
            status = resp.status_code
            resp.close()
            raise httpx.HTTPStatusError(f"Server returned HTTP {status}", request=req, response=resp)

        return resp, resp.iter_bytes(chunk_size=1024 * 1024)

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                try:
                    client.close()
                except Exception:
                    pass
            self._clients.clear()
