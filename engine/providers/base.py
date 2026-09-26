from __future__ import annotations

from typing import Any, Protocol

from ..models import ResolvedItem


class Provider(Protocol):
    id: str

    @classmethod
    def manifest(cls) -> dict[str, Any]: ...

    @classmethod
    def match(cls, url: str) -> bool: ...

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]: ...
