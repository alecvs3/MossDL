from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .provider_sdk import PROVIDER_OPERATIONS, declared_operations


class ManifestError(ValueError):
    pass


def _strip_comments(text: str) -> str:
    out: list[str] = []
    quoted = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            out.append(char)
            index += 1
        elif char == "/" and index + 1 < len(text) and text[index + 1] == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
        elif char == "/" and index + 1 < len(text) and text[index + 1] == "*":
            index += 2
            while index + 1 < len(text) and text[index:index + 2] != "*/":
                index += 1
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def loads_jsonc(text: str) -> dict[str, Any]:
    cleaned = re.sub(r",\s*([}\]])", r"\1", _strip_comments(text))

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ManifestError(f"duplicate manifest key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(cleaned, object_pairs_hook=no_duplicates)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSONC: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError("manifest root must be an object")
    return value


# Where a plugin shows in Settings -> Plugins.
PLUGIN_CATEGORIES = frozenset({"cloud", "file-host", "video", "images", "audio", "social", "any-site"})


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    required = {"schema_version", "id", "version", "hosts", "match", "capabilities", "limits"}
    missing = required - set(manifest)
    if missing:
        raise ManifestError(f"manifest missing required fields: {', '.join(sorted(missing))}")
    optional = {"roles", "permissions", "digest", "display_name", "icon", "protocol_version", "implementation", "recipe", "hooks", "operations", "fixtures", "catalog", "category"}
    unknown = set(manifest) - required - optional
    if unknown:
        raise ManifestError(f"unknown manifest fields: {', '.join(sorted(unknown))}")
    if manifest["schema_version"] not in {1, 2} or not isinstance(manifest["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", manifest["id"]):
        raise ManifestError("unsupported schema or invalid plugin id")
    if manifest.get("protocol_version", 1) not in {1, 2}:
        raise ManifestError("unsupported plugin protocol version")
    if not isinstance(manifest["hosts"], list) or not all(isinstance(item, str) for item in manifest["hosts"]):
        raise ManifestError("hosts must be a string array")
    if manifest["schema_version"] == 1 and "hooks" not in manifest:
        raise ManifestError("manifest missing required fields: hooks")
    if manifest.get("implementation", "python") not in {"python", "recipe"}:
        raise ManifestError("implementation must be python or recipe")
    if manifest.get("implementation", "python") == "recipe" and not isinstance(manifest.get("recipe"), str):
        raise ManifestError("recipe implementation requires a recipe file")
    if "category" in manifest and manifest["category"] not in PLUGIN_CATEGORIES:
        raise ManifestError(f"category must be one of: {', '.join(sorted(PLUGIN_CATEGORIES))}")
    if "icon" in manifest and (not isinstance(manifest["icon"], str) or len(manifest["icon"]) > 2048):
        raise ManifestError("icon must be a short string when provided")
    if manifest.get("implementation", "python") == "python" and not manifest.get("hooks"):
        raise ManifestError("python implementation requires hooks")
    for field in ("match", "capabilities", "limits"):
        if not isinstance(manifest[field], dict):
            raise ManifestError(f"{field} must be an object")
    if "hooks" in manifest and not isinstance(manifest["hooks"], dict):
        raise ManifestError("hooks must be an object")
    if "operations" in manifest:
        operations = manifest["operations"]
        if isinstance(operations, list):
            if not all(isinstance(item, str) and item in PROVIDER_OPERATIONS for item in operations):
                raise ManifestError("operations must contain supported provider operation names")
        elif isinstance(operations, dict):
            if not all(isinstance(item, str) and item in PROVIDER_OPERATIONS for item in operations):
                raise ManifestError("operations must contain supported provider operation names")
            if not all(isinstance(value, (bool, str, dict)) for value in operations.values()):
                raise ManifestError("operation declarations must be boolean, hook, or object values")
        else:
            raise ManifestError("operations must be a string array or object")
    if "fixtures" in manifest and not isinstance(manifest["fixtures"], (dict, str)):
        raise ManifestError("fixtures must be an object or relative fixture path")
    if "catalog" in manifest and not isinstance(manifest["catalog"], dict):
        raise ManifestError("catalog must be an object")
    unknown_match = set(manifest["match"]) - {"schemes", "host_patterns", "path_regex"}
    if unknown_match:
        raise ManifestError(f"unknown match fields: {', '.join(sorted(unknown_match))}")
    for key in ("schemes", "host_patterns"):
        if key in manifest["match"] and (not isinstance(manifest["match"][key], list) or
                                           not all(isinstance(item, str) for item in manifest["match"][key])):
            raise ManifestError(f"match.{key} must be a string array")
    path_regex = manifest["match"].get("path_regex")
    if path_regex is not None:
        try:
            re.compile(path_regex)
        except re.error as exc:
            raise ManifestError(f"invalid path_regex: {exc}") from exc
    supported = {"folders", "passwords", "refresh", "ranges", "accounts", "http", "ftp", "encrypted", "zip", "token",
                 "decrypter", "media", "browser", "notifications", "postprocess", "captcha", "metadata"}
    unknown_capabilities = set(manifest["capabilities"]) - supported
    if unknown_capabilities:
        raise ManifestError(f"unsupported capabilities: {', '.join(sorted(unknown_capabilities))}")
    roles = manifest.get("roles", ["resolver"])
    supported_roles = {"resolver", "decrypter", "downloader", "account", "postprocess", "media", "notifier"}
    if not isinstance(roles, list) or not roles or not all(isinstance(role, str) and role in supported_roles for role in roles):
        raise ManifestError("roles must be a non-empty array of supported role names")
    permissions = manifest.get("permissions", {})
    if not isinstance(permissions, dict) or set(permissions) - {"hosts", "secrets", "browser", "network", "postprocess"}:
        raise ManifestError("invalid plugin permissions")
    for field in ("hosts", "secrets"):
        if field in permissions and (not isinstance(permissions[field], list) or
                                     not all(isinstance(item, str) for item in permissions[field])):
            raise ManifestError(f"permissions.{field} must be a string array")
    for operation in ("resolve", "refresh", "authenticate", "metadata", "enumerate", "health", "extract", "extract_links"):
        hook = manifest.get("hooks", {}).get(operation)
        if hook is not None and (not isinstance(hook, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", hook)):
            raise ManifestError(f"invalid hook name: {operation}")
    # Keep this check after hook validation so legacy manifests remain valid and
    # newer manifests cannot silently advertise an unsupported operation.
    if not declared_operations(manifest).issubset(PROVIDER_OPERATIONS):
        raise ManifestError("manifest declares an unsupported provider operation")
    return manifest


def load_manifest(plugin_dir: Path) -> dict[str, Any]:
    path = plugin_dir / "manifest.jsonc"
    if not path.is_file():
        raise ManifestError(f"missing manifest: {path}")
    manifest = validate_manifest(loads_jsonc(path.read_text(encoding="utf-8")))
    if manifest["id"] != plugin_dir.name:
        raise ManifestError(f"manifest id does not match directory: {plugin_dir.name}")
    return manifest
