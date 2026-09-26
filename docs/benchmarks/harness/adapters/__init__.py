"""Client adapters. Add a client by subclassing ``base.Adapter`` and registering it here."""

from __future__ import annotations

from .abdm import ABDMAdapter
from .base import Adapter, Job, JobFile, Unavailable
from .fdm import FDMAdapter
from .idm import IDMAdapter
from .jdownloader import JDownloaderAdapter
from .mossdl import MossDLAdapter
from .motrix import Aria2RpcAdapter, MotrixAdapter

REGISTRY: dict[str, type[Adapter]] = {cls.name: cls for cls in (
    MossDLAdapter, IDMAdapter, JDownloaderAdapter, FDMAdapter, ABDMAdapter, MotrixAdapter, Aria2RpcAdapter)}


def create(name: str, options: dict | None = None) -> Adapter:
    if name not in REGISTRY:
        raise SystemExit(f"unknown client {name!r}; choose from {', '.join(REGISTRY)}")
    return REGISTRY[name](options)


__all__ = ["Adapter", "Job", "JobFile", "Unavailable", "REGISTRY", "create"]
