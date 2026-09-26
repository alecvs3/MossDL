"""Provider compatibility inventory and fixture helpers.

This module deliberately treats the Cyberdrop-DL checkout as a reference
catalog, not as an implementation dependency.  It gives us a repeatable way
to answer three different questions:

* Is a local plugin manifest valid and routable?
* Does the corresponding Cyberdrop-DL crawler and/or fixture exist?
* Has an opt-in live URL been supplied for this provider?

The distinction is important: a crawler name overlap is useful coverage
information, but it is not evidence that either implementation is healthy.
"""

from __future__ import annotations

import ast
import re
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .plugin_catalog import ManifestError, load_manifest


# Names which cannot be derived safely from the local plugin id.
CYBERDROP_CRAWLER_ALIASES: dict[str, str] = {
    "1fichier": "onefichier",
    "box": "box_dot_com",
    "fuckingfast": "fucking_fast",
    "google-drive": "google_drive",
    "mega": "mega_nz",
    "nova_storage": "nova",
    "transfer.it": "transfer_it",
    "vipr": "vipr_dot_im",
    "voe": "voe_sx",
    "wordpress_media": "wordpress",
}


@dataclass(frozen=True)
class ProviderCompatibility:
    provider_id: str
    display_name: str
    version: str
    hosts: tuple[str, ...]
    local_operations: tuple[str, ...]
    capabilities: tuple[str, ...]
    cyberdrop_crawler: str | None
    cyberdrop_overlap: bool
    reference_fixture: str | None
    reference_unit_test: str | None
    fixture_url: str
    routing_fixture_url: str
    fixture_source: str
    live_url_env: str
    manifest_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _crawler_names(crawlers_root: Path) -> set[str]:
    names = {
        path.stem for path in crawlers_root.glob("*.py")
        if not path.name.startswith("_") and path.name != "crawler.py"
    }
    names.update(
        path.name for path in crawlers_root.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    )
    return names


def _reference_file(cases_root: Path, crawler: str, prefix: str = "") -> Path | None:
    candidate = cases_root / f"{prefix}{crawler}.py"
    return candidate if candidate.is_file() else None


def _extract_urls(path: Path) -> list[str]:
    """Extract literal fixture URLs without importing reference code."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return []
    urls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        value = node.value.strip()
        if value.startswith(("http://", "https://")):
            urls.append(value)
    return urls


def _synthetic_url(manifest: dict[str, Any]) -> str:
    host = str((manifest.get("hosts") or ["example.invalid"])[0])
    if "*" in host:
        host = host.replace("*", "") or "example"
        host = f"fixture.{host}.invalid"
    scheme = str((manifest.get("match", {}).get("schemes") or ["https"])[0])
    pattern = str(manifest.get("match", {}).get("path_regex") or ".*")
    if pattern.startswith("^/("):
        path = "/file/fixture"
    else:
        match = re.match(r"\^(/[^\\[(|+*?{}]+)", pattern)
        path = match.group(1).rstrip("$") if match else "/fixture"
        if not path.startswith("/"):
            path = "/" + path
    return urllib.parse.urlunsplit((scheme, host, path, "", ""))


def _slug(provider_id: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", provider_id.upper()).strip("_")


def local_plugin_manifests(plugin_root: Path | str) -> list[tuple[str, dict[str, Any], str | None]]:
    """Return every plugin directory, including invalid manifests."""
    records: list[tuple[str, dict[str, Any], str | None]] = []
    for directory in sorted(Path(plugin_root).iterdir()):
        if not directory.is_dir():
            continue
        try:
            records.append((directory.name, load_manifest(directory), None))
        except (ManifestError, OSError, UnicodeError) as exc:
            records.append((directory.name, {}, str(exc)))
    return records


def compare_plugin_ecosystems(
    plugin_root: Path | str,
    cyberdrop_root: Path | str,
) -> list[ProviderCompatibility]:
    """Build a deterministic local-plugin vs Cyberdrop-DL comparison."""
    cyberdrop_root = Path(cyberdrop_root)
    crawler_root = cyberdrop_root / "cyberdrop_dl" / "crawlers"
    cases_root = cyberdrop_root / "tests" / "crawlers" / "test_cases"
    unit_root = cyberdrop_root / "tests" / "crawlers"
    crawlers = _crawler_names(crawler_root) if crawler_root.is_dir() else set()
    result: list[ProviderCompatibility] = []

    for provider_id, manifest, manifest_error in local_plugin_manifests(plugin_root):
        if manifest_error:
            result.append(ProviderCompatibility(
                provider_id=provider_id, display_name=provider_id, version="", hosts=(),
                local_operations=(), capabilities=(), cyberdrop_crawler=None,
                cyberdrop_overlap=False, reference_fixture=None, reference_unit_test=None,
                fixture_url="", routing_fixture_url="", fixture_source="manifest-error",
                live_url_env=f"TRANSFER_MANAGER_LIVE_URL_{_slug(provider_id)}",
                manifest_error=manifest_error,
            ))
            continue

        crawler = CYBERDROP_CRAWLER_ALIASES.get(
            provider_id, provider_id.replace("-", "_").replace(".", "_")
        )
        overlap = crawler in crawlers
        fixture_path = _reference_file(cases_root, crawler) if overlap else None
        unit_path = _reference_file(unit_root, crawler, prefix="test_") if overlap else None
        fixture_url = ""
        fixture_source = "none"
        if fixture_path:
            urls = _extract_urls(fixture_path)
            if urls:
                fixture_url, fixture_source = urls[0], "cyberdrop-test-case"
        if not fixture_url:
            fixture_url, fixture_source = _synthetic_url(manifest), "synthetic-manifest"
        routing_fixture_url = _synthetic_url(manifest)

        capabilities = manifest.get("capabilities", {})
        if isinstance(capabilities, list):
            capabilities = {str(value): True for value in capabilities}
        result.append(ProviderCompatibility(
            provider_id=provider_id,
            display_name=str(manifest.get("display_name") or provider_id),
            version=str(manifest.get("version") or ""),
            hosts=tuple(str(value) for value in manifest.get("hosts", [])),
            local_operations=tuple(sorted(str(key) for key in manifest.get("hooks", {}))),
            capabilities=tuple(sorted(str(key) for key, value in capabilities.items() if value)),
            cyberdrop_crawler=crawler if overlap else None,
            cyberdrop_overlap=overlap,
            reference_fixture=str(fixture_path) if fixture_path else None,
            reference_unit_test=str(unit_path) if unit_path else None,
            fixture_url=fixture_url,
            routing_fixture_url=routing_fixture_url,
            fixture_source=fixture_source,
            live_url_env=f"TRANSFER_MANAGER_LIVE_URL_{_slug(provider_id)}",
        ))
    return result


def compatibility_summary(records: list[ProviderCompatibility]) -> dict[str, int]:
    overlaps = [record for record in records if record.cyberdrop_overlap]
    return {
        "local_plugins": len(records),
        "manifest_errors": sum(record.manifest_error is not None for record in records),
        "cyberdrop_overlaps": len(overlaps),
        "reference_fixtures": sum(record.reference_fixture is not None for record in overlaps),
        "reference_unit_tests": sum(record.reference_unit_test is not None for record in overlaps),
    }


def compatibility_report(plugin_root: Path | str, cyberdrop_root: Path | str) -> dict[str, Any]:
    records = compare_plugin_ecosystems(plugin_root, cyberdrop_root)
    return {
        "summary": compatibility_summary(records),
        "providers": [record.to_dict() for record in records],
    }
