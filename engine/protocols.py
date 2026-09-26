"""Reusable protocol-family adapters.

Adapters return ordinary resolved items. Network enumeration is intentionally
bounded and optional dependencies are loaded only when their protocol is used.
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from typing import Any

from .models import ResolvedItem


def resolve_protocol(url: str, *, credential_ref: str | None = None) -> list[ResolvedItem]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if parsed.username or parsed.password:
        raise ValueError("embedded protocol credentials are not allowed; use credential_ref")
    if scheme in {"http", "https"} and any(key in parsed.query.lower() for key in ("x-amz-signature", "sig", "token")):
        return [ResolvedItem("object-storage", url, os.path.basename(parsed.path) or "download",
                             direct_url=url, metadata={"protocol": "signed-object-url", "credential_ref": credential_ref})]
    if scheme in {"http", "https", "webdav", "webdavs"}:
        direct = urllib.parse.urlunsplit(("https" if scheme == "webdavs" else "http" if scheme == "webdav" else scheme,
                                          parsed.netloc, parsed.path, parsed.query, ""))
        return [ResolvedItem("webdav", url, os.path.basename(parsed.path) or "download", direct_url=direct,
                             metadata={"protocol": "webdav", "credential_ref": credential_ref})]
    if scheme in {"s3", "s3+https"}:
        return [ResolvedItem("s3", url, os.path.basename(parsed.path) or "download", direct_url=None,
                             metadata={"protocol": "s3", "bucket": parsed.netloc, "key": parsed.path.lstrip("/"),
                                       "credential_ref": credential_ref})]
    if scheme == "sftp":
        return [ResolvedItem("sftp", url, os.path.basename(parsed.path) or "download", direct_url=None,
                             metadata={"protocol": "sftp", "host": parsed.hostname, "port": parsed.port or 22,
                                       "path": parsed.path, "credential_ref": credential_ref})]
    raise ValueError(f"unsupported protocol: {scheme}")
