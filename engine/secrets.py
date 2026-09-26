from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

SERVICE = "transfer-manager"


class SecretBackend(Protocol):
    def set(self, name: str, value: str) -> None: ...
    def get(self, name: str) -> str | None: ...
    def delete(self, name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Opaque durable handle; the value is intentionally never serializable."""

    scheme: str
    name: str

    def __post_init__(self) -> None:
        if self.scheme not in {"keychain", "secret"} or not self.name or any(ch in self.name for ch in "\r\n"):
            raise ValueError("invalid secret reference")

    def __str__(self) -> str:
        return f"{self.scheme}://{quote(self.name, safe='/._-')}"

    @classmethod
    def parse(cls, value: str) -> "SecretRef":
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"keychain", "secret"}:
            raise ValueError("secret references must use keychain:// or secret://")
        name = unquote(f"{parsed.netloc}{parsed.path}").lstrip("/")
        return cls(parsed.scheme, name)


class InMemorySecretBackend:
    """Deterministic fake used by tests and offline development."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


class SecretStore:
    """OS keychain wrapper; values never belong in the SQLite task record."""

    def __init__(self, service: str = SERVICE) -> None:
        try:
            import keyring
        except ImportError as exc:
            raise RuntimeError("keyring is required for persistent credentials") from exc
        self._keyring = keyring
        self.service = service

    def set(self, name: str, value: str) -> None:
        self._keyring.set_password(self.service, name, value)

    def get(self, name: str) -> str | None:
        return self._keyring.get_password(self.service, name)

    def delete(self, name: str) -> None:
        try:
            self._keyring.delete_password(self.service, name)
        except self._keyring.errors.PasswordDeleteError:
            pass


class SecretManager:
    """Single credential boundary for accounts, sessions, and capture flows."""

    def __init__(self, backend: SecretBackend | None = None, service: str = SERVICE) -> None:
        self.service = service
        self._backend = backend

    @property
    def backend(self) -> SecretBackend:
        if self._backend is None:
            try:
                self._backend = SecretStore(self.service)
            except Exception:
                self._backend = InMemorySecretBackend()
        return self._backend

    def put(self, value: str, *, kind: str = "opaque", name: str | None = None) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("secret value must be a non-empty string")
        ref = SecretRef("keychain", name or f"{kind}/{uuid4()}")
        self.backend.set(ref.name, value)
        return str(ref)

    def resolve(self, reference: str) -> str | None:
        return self.backend.get(SecretRef.parse(reference).name)

    def resolve_operation(self, reference: str | None) -> str | None:
        """Resolve one opaque reference for the lifetime of a provider call.

        Callers must not persist or return this value.  Keeping this boundary
        explicit prevents account records from becoming an accidental secret
        transport while still allowing providers to authenticate.
        """
        if not reference:
            return None
        return self.resolve(self.validate(reference))

    def delete(self, reference: str) -> str:
        ref = SecretRef.parse(reference)
        self.backend.delete(ref.name)
        return str(ref)

    def rotate(self, reference: str, value: str) -> str:
        ref = SecretRef.parse(reference)
        if not value:
            raise ValueError("secret value must be a non-empty string")
        self.backend.set(ref.name, value)
        return str(ref)

    @staticmethod
    def validate(reference: str) -> str:
        return str(SecretRef.parse(reference))
