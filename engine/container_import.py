"""Deterministic, secret-safe importers for collection container formats."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .collection import canonical_url, public_url, redact, stable_item_id

MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 10_000
SUPPORTED_FORMATS = {"text", "batch", "dlc", "crawljob"}
_URL_RE = re.compile(r"https?://[^\s<>\"']+|ftp://[^\s<>\"']+", re.IGNORECASE)
_SENSITIVE_QUERY = {"token", "access_token", "authorization", "cookie", "password", "secret", "signature", "sig"}


class ContainerImportError(ValueError):
    """Raised when an input is unsupported, malformed, or exceeds bounds."""


@dataclass(frozen=True)
class ImportRecord:
    url: str
    display_name: str = "download"
    folder_path: str = ""
    package_path: str = ""
    parent_id: str | None = None
    item_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedImport:
    format: str
    container_id: str
    collection_key: str
    source_name: str
    records: tuple[ImportRecord, ...]
    provenance: dict[str, Any]


def _safe_url(value: Any) -> str:
    text = str(value or "").strip().rstrip(",;")
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https", "ftp"} or not parsed.hostname:
        raise ContainerImportError(f"invalid URL: {text[:120]}")
    if parsed.username or parsed.password:
        raise ContainerImportError("URL credentials must use a credential reference")
    return public_url(text)


def _name(value: Any, url: str) -> str:
    text = str(value or "").strip()
    return text[:512] if text else (urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "download")


def _record(raw: Any, *, default_folder: str = "", default_package: str = "") -> ImportRecord:
    if isinstance(raw, str):
        url = _safe_url(raw)
        return ImportRecord(url, _name(None, url), default_folder, default_package)
    if not isinstance(raw, dict):
        raise ContainerImportError("container entries must be objects or URLs")
    value = raw.get("url") or raw.get("source_url") or raw.get("download_url") or raw.get("link")
    url = _safe_url(value)
    folder = str(raw.get("folder_path") or raw.get("folder") or raw.get("path") or default_folder).strip("/")
    package = str(raw.get("package_path") or raw.get("package") or default_package).strip("/")
    metadata = {str(k): v for k, v in raw.items() if str(k).lower() not in {
        "url", "source_url", "download_url", "link", "name", "display_name", "filename", "folder", "folder_path",
        "path", "package", "package_path", "item_id", "id", "parent_id", "children", "links",
    }}
    return ImportRecord(url, _name(raw.get("display_name") or raw.get("name") or raw.get("filename"), url),
                        folder, package, str(raw.get("parent_id")) if raw.get("parent_id") else None,
                        str(raw.get("item_id") or raw.get("id")) if raw.get("item_id") or raw.get("id") else None,
                        redact(metadata))


def _json_records(value: Any) -> list[ImportRecord]:
    if isinstance(value, list):
        return [_record(item) for item in value]
    if not isinstance(value, dict):
        raise ContainerImportError("JSON container must be an object or array")
    default_folder = str(value.get("folder") or value.get("folder_path") or "")
    default_package = str(value.get("package") or value.get("package_path") or "")
    entries = value.get("urls") or value.get("items") or value.get("links") or value.get("downloads") or value.get("jobs")
    if entries is None and value.get("url"):
        entries = [value]
    if not isinstance(entries, list):
        raise ContainerImportError("JSON container entries must be a list")
    return [_record(item, default_folder=default_folder, default_package=default_package) for item in entries]


def _xml_records(text: str) -> list[ImportRecord]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ContainerImportError("invalid XML container") from exc
    records: list[ImportRecord] = []
    for element in root.iter():
        value = element.attrib.get("url") or element.attrib.get("source") or (element.text or "").strip()
        if not value or not _URL_RE.match(value):
            continue
        records.append(_record({"url": value, "name": element.attrib.get("name") or element.attrib.get("filename"),
                                "folder": element.attrib.get("folder"), "package": element.attrib.get("package"),
                                "id": element.attrib.get("id")}))
    if not records:
        raise ContainerImportError("XML container contains no URLs")
    return records


def _text_records(text: str) -> list[ImportRecord]:
    records: list[ImportRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        matches = _URL_RE.findall(line)
        if len(matches) != 1 or matches[0] != line:
            raise ContainerImportError("text URL lists may contain one URL per line")
        records.append(_record(line))
    return records


def _format_for(name: str | None, text: str) -> str:
    suffix = Path(name or "").suffix.lower()
    if suffix in {".dlc"}:
        return "dlc"
    if suffix in {".crawljob"}:
        return "crawljob"
    if suffix in {".txt", ".urls", ".url"}:
        return "text"
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return "text"
    if isinstance(value, dict) and (value.get("crawljob") or value.get("jobs") or value.get("crawl")):
        return "crawljob"
    return "batch"


def normalize_import(payload: str | bytes | Path, *, name: str | None = None,
                     format: str | None = None, container_id: str | None = None,
                     max_bytes: int = MAX_INPUT_BYTES, max_entries: int = MAX_ENTRIES) -> NormalizedImport:
    source_name = name or (payload.name if isinstance(payload, Path) else "input")
    if isinstance(payload, Path):
        raw = payload.read_bytes()
        source_name = name or payload.name
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raw = str(payload).encode("utf-8")
    if len(raw) > max_bytes:
        raise ContainerImportError("container input exceeds maximum size")
    text = raw.decode("utf-8-sig")
    kind = (format or _format_for(source_name, text)).lower()
    if kind not in SUPPORTED_FORMATS:
        raise ContainerImportError(f"unsupported container format: {kind}")
    if kind == "text":
        records = _text_records(text)
    elif kind == "batch":
        try:
            records = _json_records(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ContainerImportError("batch container must be valid JSON") from exc
    elif kind in {"dlc", "crawljob"}:
        try:
            records = _json_records(json.loads(text))
        except json.JSONDecodeError:
            records = _xml_records(text)
    else:
        records = []
    if not records:
        raise ContainerImportError("container contains no URLs")
    if len(records) > max_entries:
        raise ContainerImportError("container contains too many entries")
    deduped: list[ImportRecord] = []
    seen: set[str] = set()
    for record in records:
        identity = canonical_url(record.url)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(record)
    safe_identity = "\n".join(f"{canonical_url(r.url)}\0{r.folder_path}\0{r.package_path}" for r in deduped)
    digest = hashlib.sha256(f"{kind}\0{container_id or ''}\0{safe_identity}".encode()).hexdigest()[:32]
    stable_container = str(container_id or digest)
    collection_digest = hashlib.sha256(f"{kind}\0{stable_container}".encode()).hexdigest()[:32]
    return NormalizedImport(kind, stable_container, f"container-{collection_digest}", source_name, tuple(deduped),
                            {"source_format": kind, "container_id": stable_container, "source_name": source_name})


def records_as_graph(imported: NormalizedImport, provider_id: str | None = "generic") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    parents: dict[tuple[str, str], str] = {}
    for record in imported.records:
        parent_id = record.parent_id
        parts = [part for part in (record.package_path, record.folder_path) if part]
        for depth, part in enumerate(parts):
            key = ("/".join(parts[:depth + 1]), "folder")
            parent_id = parents.setdefault(key, "folder-" + hashlib.sha256((imported.container_id + "\0" + key[0]).encode()).hexdigest()[:24])
            if not any(node["node_id"] == parent_id for node in nodes):
                nodes.append({"node_id": parent_id, "parent_id": None if depth == 0 else parents[("/".join(parts[:depth]), "folder")],
                              "depth": depth, "source_url": "container://" + imported.container_id,
                              "canonical_url": "container://" + imported.container_id, "page": 0, "cursor": None,
                              "folder_path": "/".join(parts[:depth + 1]), "package_path": record.package_path,
                              "display_name": part, "provider": provider_id, "item_id": None, "size": None,
                              "status": "container", "outcome": None, "metadata": imported.provenance})
        node_id = stable_item_id(provider_id, record.url, record.item_id)
        if any(node["node_id"] == node_id for node in nodes):
            events.append({"outcome": "duplicate", "node_id": node_id})
            continue
        nodes.append({"node_id": node_id, "parent_id": parent_id, "depth": len(parts), "source_url": record.url,
                      "canonical_url": canonical_url(record.url), "page": 0, "cursor": None,
                      "folder_path": record.folder_path, "package_path": record.package_path,
                      "display_name": record.display_name, "provider": provider_id, "item_id": record.item_id,
                      "size": None, "status": "discovered", "outcome": None,
                      "metadata": redact({**record.metadata, **imported.provenance})})
    return nodes, events
