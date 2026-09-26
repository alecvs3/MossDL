"""pyLoad Hoster Ingestion & Execution Adapter.

Provides a lightweight hoster compatibility layer for pyLoad-style provider plugins.
Emulates pyLoad's Hoster interface, URL patterns, form handlers, and error types,
allowing community hoster scripts to run natively inside Transfer Manager's plugin workers
without installing pyload-ng or external daemons.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Callable, Type
from engine.errors import NeedsCaptcha, NeedsUser, ProviderMappedError, ProviderUnavailable
from engine.models import ResolvedItem


class PyLoadHosterStub:
    """Base compatibility class mimicking pyload's module.plugins.Hoster."""

    __name__ = "PyLoadHosterStub"
    __version__ = "1.0.0"
    __type__ = "hoster"
    __pattern__ = r"^https?://.*"
    __description__ = "Generic pyLoad Hoster Stub"
    __license__ = "GPLv3"
    __authors__ = [("Transfer Manager Team", "support@transfermanager.local")]

    def __init__(self, url: str, html: str = "", req_fn: Callable[..., Any] | None = None) -> None:
        self.url = url
        self.html = html
        self.req_fn = req_fn
        self.last_download_url: str | None = None
        self.filename: str | None = None
        self.filesize: int | None = None

    def process(self, url: str) -> str:
        """Process URL and return direct download URL. Override in subclass."""
        raise NotImplementedError("process() must be implemented by hoster")

    def load(self, url: str, post: dict[str, Any] | None = None, req=None) -> str:
        """Fetch URL content."""
        if self.req_fn:
            res = self.req_fn(url, post=post)
            self.html = res if isinstance(res, str) else getattr(res, "text", str(res))
            return self.html
        return ""

    def correct_file_name(self, name: str) -> str:
        return re.sub(r"[\\/:*?\"<>|]", "_", name).strip()

    def offline(self) -> None:
        raise ProviderMappedError("File not found or link has expired", "not_found", 404)

    def temp_offline(self) -> None:
        raise ProviderMappedError("File is temporarily offline", "retryable", 503)

    def fail(self, msg: str = "Download failed") -> None:
        raise ProviderUnavailable(msg)


class PyLoadPluginAdapter:
    """Adapts a pyLoad Hoster class into Transfer Manager's standard JSON-RPC hook operations."""

    def __init__(self, hoster_cls: Type[PyLoadHosterStub], req_fn: Callable[..., Any] | None = None) -> None:
        self.hoster_cls = hoster_cls
        self.req_fn = req_fn

    def can_handle(self, url: str) -> bool:
        pattern = getattr(self.hoster_cls, "__pattern__", None)
        if not pattern:
            return False
        return bool(re.search(pattern, url, re.I))

    def resolve(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = params.get("url") or ""
        hoster = self.hoster_cls(url, req_fn=self.req_fn)
        direct_url = hoster.process(url)

        # Fallback to direct_url or last_download_url
        final_url = direct_url or hoster.last_download_url or url
        filename = hoster.filename or urllib.parse.unquote(url.rstrip("/").split("/")[-1]) or "download"

        # Use custom class attribute __name__ if defined in dict, else class name
        provider_name = self.hoster_cls.__dict__.get("__name__") or getattr(self.hoster_cls, "__name__", "pyload_hoster")

        item = ResolvedItem(
            provider=str(provider_name).lower(),
            source_url=url,
            display_name=filename,
            relative_path=filename,
            size=hoster.filesize,
            direct_url=final_url,
            metadata={"source": "pyload_adapter", "version": getattr(self.hoster_cls, "__version__", "1.0.0")},
        )
        return [item.to_dict()]

    def refresh(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        item = params.get("item") or {}
        source_url = item.get("source_url") or params.get("url")
        return self.resolve({**params, "url": source_url})
