"""Passive, bounded classification of already-received anti-bot responses.

This module deliberately has no transport dependency.  Callers give it response
facts they have already received; it never probes a URL or invokes a solver.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .telemetry import telemetry_bus

_MAX_INSPECTION_BYTES = 64 * 1024
_TEXT_CONTENT_TYPES = ("text/", "application/xhtml", "application/json", "application/javascript")


@dataclass(frozen=True)
class ChallengeEvidence:
    """A stable, public evidence code; never carries remote response material."""

    code: str


@dataclass(frozen=True)
class ChallengeVerdict:
    """Append-only public result of passive response classification."""

    outcome: str
    family: str | None
    challenge_type: str | None
    confidence: str
    evidence: tuple[ChallengeEvidence, ...]
    required_capability: str | None
    artifact_scope: str | None
    catalog_version: str
    automation_eligible: bool


@dataclass(frozen=True)
class ResponseObservation:
    """Normalized received response facts supplied by an adapter boundary."""

    status: int | None
    headers: Mapping[str, str]
    cookie_names: tuple[str, ...]
    final_url: str
    body_prefix: bytes = b""


def _load_catalog() -> dict[str, Any]:
    path = Path(__file__).with_name("challenge_rules.json")
    with path.open("r", encoding="utf-8") as handle:
        catalog = json.load(handle)
    if catalog.get("catalog") != "challenge-rules/1" or not isinstance(catalog.get("rules"), list):
        raise ValueError("invalid bundled challenge rules catalog")
    return catalog


_CATALOG = _load_catalog()
CATALOG_VERSION = _CATALOG["catalog"]
_RULES = tuple(_CATALOG["rules"])


def response_observation(
    *, status: int | None, headers: Mapping[str, Any] | None, final_url: str,
    body_prefix: bytes | bytearray | None = None,
) -> ResponseObservation:
    """Create a redacted observation from facts a transport already received."""
    normalized_headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    cookie_names = tuple(sorted(
        part.split("=", 1)[0].strip().lower()
        for value in (normalized_headers.get("set-cookie", ""),)
        for part in value.split(",") if "=" in part
    ))
    prefix = bytes(body_prefix or b"")[:_MAX_INSPECTION_BYTES]
    return ResponseObservation(status, normalized_headers, cookie_names, str(final_url or ""), prefix)


def is_inspectable_text(headers: Mapping[str, Any] | None) -> bool:
    content_type = next(
        (str(value).lower() for key, value in (headers or {}).items() if str(key).lower() == "content-type"),
        "",
    )
    return any(content_type.startswith(kind) for kind in _TEXT_CONTENT_TYPES)


def classify(observation: ResponseObservation) -> ChallengeVerdict:
    """Classify local response facts only; no I/O or transport is performed."""
    status = observation.status or 0
    if status == 429:
        return _verdict("rate_limited", confidence="high", evidence=("http-429",))
    if status in (401, 407) or status == 403 and "login" in observation.final_url.lower():
        return _verdict("authentication", confidence="high", evidence=("authentication-status",))
    content_type = observation.headers.get("content-type", "")
    if content_type and not is_inspectable_text(observation.headers):
        return _verdict("binary", confidence="high", evidence=("binary-content-type",))
    text = observation.body_prefix.decode("utf-8", "ignore").lower()
    for rule in _RULES:
        if status < int(rule.get("minimum_status", 0)):
            continue
        markers = tuple(str(marker).lower() for marker in rule.get("body_markers", ()))
        if markers and not any(marker in text for marker in markers):
            continue
        return ChallengeVerdict(
            outcome="challenge", family=rule["family"], challenge_type=rule["challenge_type"],
            confidence=str(rule.get("confidence", "high")),
            evidence=tuple(ChallengeEvidence(code) for code in (
                "status-error" if status >= 400 else "in-page-widget", f"rule:{rule['id']}")),
            required_capability=rule["required_capability"], artifact_scope=rule["artifact_scope"],
            catalog_version=CATALOG_VERSION,
            # Only high-confidence evidence may start automation; in-page widgets are
            # left to the provider flow that owns the page.
            automation_eligible=bool(rule.get("automation", True)) and rule.get("confidence", "high") == "high",
        )
    if text or (content_type and is_inspectable_text(observation.headers)):
        return _verdict("ordinary_html", confidence="low", evidence=("inspectable-text",))
    return _verdict("no_match", confidence="low", evidence=())


def observe_received_response(
    *, source: str, status: int | None, headers: Mapping[str, Any] | None,
    final_url: str, body_prefix: bytes | bytearray | None = None,
) -> ChallengeVerdict:
    """Classify one received response and emit secret-free diagnostics.

    ``source`` names an existing transport boundary.  This helper is intentionally
    transport-free: calling it cannot cause a follow-up request or persistent write.
    """
    verdict = classify(response_observation(
        status=status, headers=headers, final_url=final_url, body_prefix=body_prefix,
    ))
    telemetry_bus.record(
        level="INFO" if verdict.outcome == "challenge" else "DEBUG",
        subsystem="engine:challenge_detector",
        message=f"[CHALLENGE_OBSERVED] {source}: {verdict.outcome}",
        context={
            "source": source, "status": status, "outcome": verdict.outcome,
            "family": verdict.family, "challenge_type": verdict.challenge_type,
            "confidence": verdict.confidence, "evidence_codes": [item.code for item in verdict.evidence],
            "catalog_version": verdict.catalog_version,
        },
        tier="engine",
    )
    return verdict


def _verdict(outcome: str, *, confidence: str, evidence: tuple[str, ...]) -> ChallengeVerdict:
    return ChallengeVerdict(
        outcome=outcome, family=None, challenge_type=None, confidence=confidence,
        evidence=tuple(ChallengeEvidence(code) for code in evidence), required_capability=None,
        artifact_scope=None, catalog_version=CATALOG_VERSION, automation_eligible=False,
    )


class ReplayInspectableStream:
    """One byte-preserving iterator for both ``read`` and iterator consumers."""

    def __init__(self, source: Iterable[bytes], observation: ResponseObservation, inspect: bool, *, source_name: str = "http_client.stream") -> None:
        self._source = iter(source)
        self._observation = observation
        self._inspect = inspect
        self._source_name = source_name
        self._prepared = False
        self._buffer = bytearray()
        self._pending = b""
        self.verdict: ChallengeVerdict | None = None

    def _prepare(self) -> None:
        if self._prepared:
            return
        self._prepared = True
        if not self._inspect:
            self.verdict = observe_received_response(
                source=self._source_name, status=self._observation.status, headers=self._observation.headers,
                final_url=self._observation.final_url,
            )
            return
        while len(self._buffer) < _MAX_INSPECTION_BYTES:
            try:
                chunk = next(self._source)
            except StopIteration:
                break
            if chunk:
                remaining = _MAX_INSPECTION_BYTES - len(self._buffer)
                self._buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._pending = chunk[remaining:]
                    break
        prefix = bytes(self._buffer[:_MAX_INSPECTION_BYTES])
        self.verdict = observe_received_response(
            source=self._source_name, status=self._observation.status, headers=self._observation.headers,
            final_url=self._observation.final_url, body_prefix=prefix,
        )

    def iter_bytes(self) -> Iterator[bytes]:
        self._prepare()
        if self._buffer:
            yield bytes(self._buffer)
            self._buffer.clear()
        if self._pending:
            yield self._pending
            self._pending = b""
        yield from self._source

    def read(self, amt: int | None = None) -> bytes:
        self._prepare()
        if amt is None or amt < 0:
            return b"".join(self.iter_bytes())
        if self._pending and len(self._buffer) < amt:
            take = amt - len(self._buffer)
            self._buffer.extend(self._pending[:take])
            self._pending = self._pending[take:]
        while len(self._buffer) < amt:
            try:
                chunk = next(self._source)
            except StopIteration:
                break
            if chunk:
                take = amt - len(self._buffer)
                self._buffer.extend(chunk[:take])
                if len(chunk) > take:
                    self._pending = chunk[take:]
        result = bytes(self._buffer[:amt])
        del self._buffer[:amt]
        return result
