from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .plugin_catalog import load_manifest
from .plugins import PluginProcess
from .provider_sdk import PROVIDER_CAPABILITY_OPERATIONS, declared_operations, validate_operation_envelope
from .recipe_runtime import load_recipe


def _has_operation(plugin_dir: Path, manifest: dict[str, Any], operation: str) -> bool:
    if manifest.get("hooks", {}).get(operation):
        return True
    if manifest.get("implementation") == "recipe":
        return operation in load_recipe(plugin_dir, manifest).get("operations", {})
    return False


def _test_urls(manifest: dict[str, Any]) -> tuple[str, str]:
    host = manifest["hosts"][0] if manifest["hosts"] else "example.invalid"
    if host == "*":
        host = "contract.example"
    elif "*" in host:
        # Build a hostname that satisfies wildcard host patterns such as
        # ``*owncloud*``.  ``contract.example`` is not a valid positive case
        # for those patterns and caused generated/provider contract checks to
        # report false failures.
        stem = re.sub(r"[^A-Za-z0-9.-]+", "", host).strip(".") or "contract"
        host = f"{stem}.example"
    scheme = (manifest.get("match", {}).get("schemes") or ["https"])[0]
    path_regex = manifest.get("match", {}).get("path_regex", "")
    path = "/fixture"
    if "/d/" in path_regex:
        path = "/d/fixture"
    elif "/t/" in path_regex:
        path = "/t/fixture"
    elif "file|folder" in path_regex:
        path = "/file/fixture"
    positive = f"{scheme}://{host}{path}"
    # A syntactically invalid URL is guaranteed to be outside every manifest
    # matcher, including the intentionally catch-all generic provider.
    negative = "not-a-valid-url"
    return positive, negative


def validate_plugin_contract(plugin_dir: str | Path) -> dict[str, Any]:
    path = Path(plugin_dir).resolve()
    manifest = load_manifest(path)
    implementation = manifest.get("implementation", "python")
    if implementation == "recipe":
        recipe = load_recipe(path, manifest)
        operations = set(recipe.get("operations", {}))
    else:
        operations = set(manifest.get("hooks", {}))
    if "resolver" in manifest.get("roles", ["resolver"]) and "resolve" not in operations:
        raise AssertionError(f"{manifest['id']}: resolver has no resolve operation")
    if manifest.get("capabilities", {}).get("folders") and "enumerate" not in operations:
        raise AssertionError(f"{manifest['id']}: folders capability requires enumerate")
    if manifest.get("capabilities", {}).get("refresh") and "refresh" not in operations:
        raise AssertionError(f"{manifest['id']}: refresh capability requires refresh")
    for capability, operation in PROVIDER_CAPABILITY_OPERATIONS.items():
        if manifest.get("capabilities", {}).get(capability) and operation not in operations:
            raise AssertionError(f"{manifest['id']}: {capability} capability requires {operation}")
    if not declared_operations(manifest).issubset(operations):
        raise AssertionError(f"{manifest['id']}: manifest operation declaration has no implementation")
    process = PluginProcess(path)
    try:
        positive, negative = _test_urls(manifest)
        if process.call("match", {"url": positive}) is not True:
            raise AssertionError(f"{manifest['id']}: positive match failed for {positive}")
        if process.call("match", {"url": negative}) is True:
            raise AssertionError(f"{manifest['id']}: negative match unexpectedly succeeded for {negative}")
    finally:
        process.close()
    return {"id": manifest["id"], "implementation": implementation, "operations": sorted(operations), "status": "passed"}


def run_contracts(provider_id: str | None = None, root: str | Path | None = None) -> list[dict[str, Any]]:
    base = Path(root).resolve() if root else Path(__file__).resolve().parents[1] / "plugins"
    directories = [base / provider_id] if provider_id else sorted(item for item in base.iterdir() if item.is_dir())
    results = []
    for directory in directories:
        if not directory.is_dir():
            continue
        results.append(validate_plugin_contract(directory))
    return results
