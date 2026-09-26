"""Deterministic Cyberdrop-DL provider inventory and staged adapter generation.

The Cyberdrop-DL checkout is treated as a reference catalog.  This module
only parses source and fixture literals with :mod:`ast`; it never imports or
executes reference code.  Generated hooks call the transfer-manager provider
classes, so the generated plugin remains inside the normal isolated plugin
worker boundary.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .plugin_catalog import ManifestError, load_manifest
from .provider_compatibility import CYBERDROP_CRAWLER_ALIASES, compare_plugin_ecosystems, local_plugin_manifests


STANDARD_OPERATIONS = ("match", "metadata", "enumerate", "resolve", "refresh", "authenticate", "health")
_SECRET_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "cookie", "expires", "expiry",
    "key", "password", "pwd", "signature", "sig", "token", "x-amz-credential", "x-amz-signature",
}
_HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}


def _literal(node: ast.AST | None, default: Any = None) -> Any:
    if node is None:
        return default
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return default


def _name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _safe_url(value: str) -> str:
    """Keep fixture source URLs useful while dropping obvious secret values."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    safe_query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                  if key.lower() not in _SECRET_QUERY_KEYS]
    # Fragments can contain access/decryption keys even when they are not
    # formatted as query parameters (for example public MEGA folder keys).
    # Fixture expectations only need routing shape, never the access secret.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(safe_query), ""))


def _safe_expected(value: Any) -> Any:
    """Normalize fixture expectations without retaining signed direct URLs."""
    if isinstance(value, dict):
        allowed = {"filename", "original_filename", "referer", "download_folder", "album_id",
                   "uploaded_at", "count", "fail", "description", "debrid_url", "thumbnail", "url"}
        result: dict[str, Any] = {}
        for key, child in value.items():
            if key not in allowed:
                continue
            if key in {"url", "referer", "thumbnail"} and isinstance(child, str):
                if child.startswith("re:"):
                    result[key] = "re:" + re.sub(r"https?://[^/]+", "https://reference.invalid", child[3:])
                else:
                    result[key] = _safe_url(child)
            else:
                result[key] = _safe_expected(child)
        return result
    if isinstance(value, list):
        return [_safe_expected(child) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _assignment_map(tree: ast.Module) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = [target for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        value = _literal(node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None)
        for target in targets:
            values[target.id] = value
    return values


def _class_assignment_map(node: ast.ClassDef) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for child in node.body:
        if isinstance(child, ast.Assign):
            value = _literal(child.value)
            for target in child.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = value
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            values[child.target.id] = _literal(child.value)
    return values


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _name(node.func) or "call"
    return _name(node) or "decorator"


def _extract_rate_limits(class_node: ast.ClassDef) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for decorator in class_node.decorator_list:
        if _decorator_name(decorator).endswith("HTTPConfig") and isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if keyword.arg == "rate_limit":
                    value = _literal(keyword.value)
                    if isinstance(value, (list, tuple)) and len(value) == 2:
                        result["requests_per_second"] = round(float(value[0]) / float(value[1]), 6)
                        result["rate_limit_window_seconds"] = int(value[1])
    return result


def _extract_request_contracts(tree: ast.AST) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = _name(node.func) or ""
        if not any(part in function.lower() for part in ("request", "fetch", "download", "api")):
            continue
        method = "GET"
        literals: list[str] = []
        for keyword in node.keywords:
            if keyword.arg == "method":
                candidate = _literal(keyword.value)
                if isinstance(candidate, str) and candidate.upper() in _HTTP_METHODS:
                    method = candidate.upper()
            value = _literal(keyword.value)
            if isinstance(value, str):
                literals.append(value)
        for argument in node.args:
            value = _literal(argument)
            if isinstance(value, str):
                literals.append(value)
        urls = [value for value in literals if value.startswith(("http://", "https://", "/"))]
        if urls:
            requests.append({"method": method, "function": function, "templates": sorted(set(urls))})
    return sorted({json.dumps(value, sort_keys=True): value for value in requests}.values(),
                  key=lambda value: (value["function"], value["method"], value["templates"]))


def _extract_selectors(tree: ast.AST) -> list[str]:
    selectors: set[str] = set()
    for node in ast.walk(tree):
        value = _literal(node)
        if not isinstance(value, str) or len(value) > 240:
            continue
        if any(marker in value for marker in ("#", ".", "[", ":", "::")) and re.fullmatch(r"[\w#.:\-\[\]=\"' >+~]+", value):
            selectors.add(value.strip())
    return sorted(selectors)


def _find_crawler(tree: ast.Module) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name.endswith("Crawler") and node.name != "Crawler":
            return node
    return None


def _reference_sources(crawler_root: Path, crawler: str) -> list[Path]:
    """Return the single-file or package sources for a registered crawler."""
    direct = crawler_root / f"{crawler}.py"
    if direct.is_file():
        return [direct]
    package = crawler_root / crawler
    if package.is_dir():
        return sorted(package.rglob("*.py"))
    return []


def _best_crawler_node(trees: list[tuple[Path, ast.Module]], provider_id: str) -> tuple[Path, ast.ClassDef] | None:
    """Choose the concrete class most closely matching the local provider."""
    tokens = {part for part in re.split(r"[^a-z0-9]+", provider_id.lower()) if part}
    candidates: list[tuple[int, Path, ast.ClassDef]] = []
    for path, tree in trees:
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or not node.name.endswith("Crawler"):
                continue
            name = node.name.removesuffix("Crawler").lower()
            score = 0
            if name == provider_id.replace("-", "").replace("_", "").lower():
                score += 100
            if any(token and token in name for token in tokens):
                score += 20
            if any(_decorator_name(item).endswith("is_abc") for item in node.decorator_list):
                score -= 10
            if node.name.endswith("BaseCrawler"):
                score -= 5
            candidates.append((score, path, node))
    if not candidates:
        return None
    _, path, node = max(candidates, key=lambda item: (item[0], str(item[1])))
    return path, node


def _find_local_hook_target(path: Path) -> dict[str, str] | None:
    hook = path / "hooks.py"
    if not hook.is_file():
        return None
    try:
        tree = ast.parse(hook.read_text(encoding="utf-8"), filename=str(hook))
    except (OSError, SyntaxError, UnicodeError):
        return None
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("engine.providers"):
            for alias in node.names:
                if alias.name.endswith("Provider"):
                    return {"module": node.module, "class": alias.name}
    return None


def _fixture_cases(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return []
    values = _assignment_map(tree).get("TEST_CASES", [])
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    for value in values:
        normalized = _safe_expected(value)
        if isinstance(normalized, dict):
            result.append(normalized)
    return result


def _relative(root: Path, path: Path | None) -> str | None:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/") if path else None


@dataclass(frozen=True)
class CyberdropCrawlerSpec:
    provider_id: str
    crawler: str
    source_file: str
    fixture_file: str | None
    crawler_class: str | None
    domain: str | None
    supported_domains: tuple[str, ...] = ()
    supported_paths: dict[str, Any] = field(default_factory=dict)
    local_operations: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    decorators: tuple[str, ...] = ()
    request_contracts: tuple[dict[str, Any], ...] = ()
    selectors: tuple[str, ...] = ()
    rate_limits: dict[str, Any] = field(default_factory=dict)
    fixture_cases: tuple[dict[str, Any], ...] = ()
    local_hook_target: dict[str, str] | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_specs(plugin_root: Path | str, cyberdrop_root: Path | str) -> list[CyberdropCrawlerSpec]:
    """Build specs for every local plugin with a matching Cyberdrop crawler."""
    plugin_root = Path(plugin_root).resolve()
    reference_root = Path(cyberdrop_root).resolve()
    crawler_root = reference_root / "cyberdrop_dl" / "crawlers"
    cases_root = reference_root / "tests" / "crawlers" / "test_cases"
    specs: list[CyberdropCrawlerSpec] = []
    for provider_id, manifest, manifest_error in local_plugin_manifests(plugin_root):
        if manifest_error:
            continue
        crawler = CYBERDROP_CRAWLER_ALIASES.get(provider_id, provider_id.replace("-", "_").replace(".", "_"))
        source_paths = _reference_sources(crawler_root, crawler)
        if not source_paths:
            continue
        source = source_paths[0]
        fixture = cases_root / f"{crawler}.py"
        trees: list[tuple[Path, ast.Module]] = []
        parse_error: str | None = None
        for source_path in source_paths:
            try:
                trees.append((source_path, ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))))
            except (OSError, SyntaxError, UnicodeError) as exc:
                parse_error = str(exc)
        selected = _best_crawler_node(trees, provider_id)
        if selected is None:
            if parse_error is None:
                parse_error = "no crawler class found"
            specs.append(CyberdropCrawlerSpec(provider_id, crawler, _relative(reference_root, source) or str(source),
                                              _relative(reference_root, fixture) if fixture.is_file() else None,
                                              None, None, warnings=(f"reference parse failed: {parse_error}",)))
            continue
        source, crawler_node = selected
        tree = next(tree for path, tree in trees if path == source)
        values = _class_assignment_map(crawler_node) if crawler_node else {}
        methods = sorted({node.name for node in ast.walk(crawler_node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}) if crawler_node else []
        local_operations = set(str(key) for key in (manifest.get("hooks") or {}))
        operations = set(local_operations)
        if crawler_node and any(name in methods for name in ("folder", "album", "gallery")):
            operations.add("enumerate")
        if crawler_node and "fetch" in methods:
            operations.add("resolve")
        operations &= set(STANDARD_OPERATIONS)
        supported_domains = values.get("SUPPORTED_DOMAINS", ())
        if isinstance(supported_domains, str):
            supported_domains = (supported_domains,)
        if not isinstance(supported_domains, (list, tuple)):
            supported_domains = ()
        supported_paths = values.get("SUPPORTED_PATHS", {})
        if not isinstance(supported_paths, dict):
            supported_paths = {}
        warnings: list[str] = []
        if not _find_local_hook_target(plugin_root / provider_id):
            warnings.append("no provider-class hook target; manual adapter required")
        if "authenticate" in methods and "authenticate" not in operations:
            warnings.append("reference exposes authentication behavior not declared locally")
        missing_local = sorted(operations - local_operations)
        if missing_local:
            warnings.append("reference operations not implemented locally: " + ", ".join(missing_local))
        specs.append(CyberdropCrawlerSpec(
            provider_id=provider_id,
            crawler=crawler,
            source_file=_relative(reference_root, source) or str(source),
            fixture_file=_relative(reference_root, fixture) if fixture.is_file() else None,
            crawler_class=crawler_node.name if crawler_node else None,
            domain=values.get("DOMAIN"),
            supported_domains=tuple(str(item) for item in supported_domains),
            supported_paths=supported_paths,
            local_operations=tuple(sorted(local_operations & set(STANDARD_OPERATIONS))),
            operations=tuple(sorted(operations)),
            methods=tuple(methods),
            decorators=tuple(sorted(_decorator_name(item) for item in crawler_node.decorator_list)) if crawler_node else (),
            request_contracts=tuple(_extract_request_contracts(ast.Module(
                body=[node for _, parsed in trees for node in parsed.body], type_ignores=[]))),
            selectors=tuple(_extract_selectors(ast.Module(
                body=[node for _, parsed in trees for node in parsed.body], type_ignores=[]))),
            rate_limits=_extract_rate_limits(crawler_node) if crawler_node else {},
            fixture_cases=tuple(_fixture_cases(fixture)),
            local_hook_target=_find_local_hook_target(plugin_root / provider_id),
            warnings=tuple(sorted(set(warnings))),
        ))
    return sorted(specs, key=lambda spec: spec.provider_id)


def _manifest_for(provider_id: str, plugin_root: Path) -> dict[str, Any]:
    manifest = load_manifest(plugin_root / provider_id)
    keep = {"schema_version", "roles", "id", "version", "display_name", "hosts", "match", "capabilities",
            "limits", "permissions", "protocol_version", "implementation", "recipe", "hooks"}
    result = {key: manifest[key] for key in sorted(set(manifest) & keep)}
    result["schema_version"] = 2
    result["implementation"] = "python"
    result.pop("recipe", None)
    result["hooks"] = {key: value for key, value in (manifest.get("hooks") or {}).items() if key in STANDARD_OPERATIONS}
    result["version"] = f"cyberdrop-import-{manifest.get('version', '0.0.0')}"
    result.setdefault("roles", ["resolver"])
    result.setdefault("permissions", {"hosts": result.get("hosts", []), "secrets": []})
    return result


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _generated_hooks(spec: CyberdropCrawlerSpec) -> str | None:
    target = spec.local_hook_target
    if not target:
        return None
    provider_class = target["class"]
    lines = [
        '"""Generated provider adapter from a Cyberdrop-DL parity specification.',
        "",
        "This file uses the transfer-manager provider implementation. The reference",
        "crawler is represented by parity.json and is never imported at runtime.",
        '"""',
        "",
        f"from {target['module']} import {provider_class}",
        "",
        f"_PROVIDER = {provider_class}",
        "",
        "def _items(operation, params):",
        "    method = getattr(_PROVIDER, operation)",
        "    if operation == 'refresh':",
        "        item = params.get('item') or {}",
        "        url = item.get('source_url') or params.get('url')",
        "    else:",
        "        url = params.get('url')",
        "    result = method(url, params.get('secrets'))",
        "    return [item.to_dict() for item in result]",
        "",
    ]
    for operation in STANDARD_OPERATIONS:
        if operation == "match":
            continue
        if operation in spec.local_operations and operation != "refresh":
            lines.extend([f"def {operation}(params):", f"    return _items('{operation}', params)", ""])
    if "refresh" in spec.local_operations:
        lines.extend([
            "def refresh(params):",
            "    item = params.get('item') or {}",
            "    return resolve({'url': item.get('source_url') or params.get('url'),",
            "                    'secrets': params.get('secrets')})",
            "",
        ])
    return "\n".join(lines)


def generate_staging(plugin_root: Path | str, cyberdrop_root: Path | str, staging_root: Path | str) -> dict[str, Any]:
    """Generate staged plugin directories and return a deterministic report."""
    plugin_root = Path(plugin_root).resolve()
    reference_root = Path(cyberdrop_root).resolve()
    staging_root = Path(staging_root).resolve()
    staging_root.mkdir(parents=True, exist_ok=True)
    specs = build_specs(plugin_root, reference_root)
    spec_ids = {spec.provider_id for spec in specs}
    providers: list[dict[str, Any]] = []
    for spec in specs:
        source_plugin = plugin_root / spec.provider_id
        destination = staging_root / spec.provider_id
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        manifest = _manifest_for(spec.provider_id, plugin_root)
        (destination / "manifest.jsonc").write_text(_json_text(manifest), encoding="utf-8")
        hooks = _generated_hooks(spec)
        status = "generated" if hooks else "needs_manual_adapter"
        if hooks:
            (destination / "hooks.py").write_text(hooks, encoding="utf-8")
        else:
            (destination / "hooks.py").write_text(
                '"""Manual adapter required; generated intentionally fails closed."""\n\n'
                'def health(params):\n    return {"healthy": False, "reason": "manual_adapter_required"}\n',
                encoding="utf-8",
            )
        (destination / "fixtures").mkdir()
        (destination / "fixtures" / "reference.json").write_text(
            _json_text({"provider_id": spec.provider_id, "crawler": spec.crawler,
                        "cases": list(spec.fixture_cases)}), encoding="utf-8")
        parity = {
            "schema": 1,
            "provider_id": spec.provider_id,
            "crawler": spec.crawler,
            "reference": {"source_file": spec.source_file, "fixture_file": spec.fixture_file},
            "spec": spec.to_dict(),
            "generated": {"status": status, "source_sha256": hashlib.sha256((source_plugin / "hooks.py").read_bytes()).hexdigest()
                          if (source_plugin / "hooks.py").is_file() else None},
            "promotion": {"eligible": False, "reason": "not tested"},
        }
        (destination / "parity.json").write_text(_json_text(parity), encoding="utf-8")
        providers.append({"provider_id": spec.provider_id, "crawler": spec.crawler, "status": status,
                          "fixture_cases": len(spec.fixture_cases), "operations": list(spec.operations),
                          "warnings": list(spec.warnings)})
    local_only: list[dict[str, Any]] = []
    for provider_id, manifest, manifest_error in local_plugin_manifests(plugin_root):
        if provider_id in spec_ids or manifest_error:
            continue
        if provider_id != "vikingfile":
            continue
        local_only.append({
            "provider_id": provider_id,
            "status": "needs_fixture_validation",
            "reference": None,
            "local_operations": sorted((manifest.get("hooks") or {}).keys()),
            "fixture_candidates": [str(path.relative_to(plugin_root.parent)).replace("\\", "/")
                                   for path in (plugin_root.parent / "viking.har", plugin_root.parent / "hars" / "viking.har")
                                   if path.is_file()],
            "reason": "Cyberdrop-DL has no Vikingfile crawler; validate against local implementation and HAR fixtures.",
        })
    report = {
        "schema": 1,
        "generated_at": "deterministic",
        "reference": "clone_reference/cyberdrop-dl",
        "summary": {
            "providers": len(providers),
            "generated": sum(item["status"] == "generated" for item in providers),
            "needs_manual_adapter": sum(item["status"] == "needs_manual_adapter" for item in providers),
            "fixture_cases": sum(item["fixture_cases"] for item in providers),
            "local_only_audits": len(local_only),
        },
        "providers": providers,
        "local_only_audits": local_only,
    }
    (staging_root / "migration-report.json").write_text(_json_text(report), encoding="utf-8")
    return report


def test_staging(staging_root: Path | str) -> dict[str, Any]:
    """Run manifest/plugin-worker contracts against staged adapters."""
    from .plugin_contracts import validate_plugin_contract

    staging_root = Path(staging_root).resolve()
    report_path = staging_root / "migration-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {"providers": []}
    results: list[dict[str, Any]] = []
    for provider in report.get("providers", []):
        provider_id = provider["provider_id"]
        directory = staging_root / provider_id
        try:
            result = validate_plugin_contract(directory)
            status = "passed" if provider["status"] == "generated" else "needs_manual_adapter"
            results.append({"provider_id": provider_id, "status": status, "contract": result})
        except Exception as exc:  # contract reports must continue across all providers
            results.append({"provider_id": provider_id, "status": "failed", "error": str(exc)})
    summary = {
        "providers": len(results),
        "passed": sum(item["status"] == "passed" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "needs_manual_adapter": sum(item["status"] == "needs_manual_adapter" for item in results),
    }
    output = {"schema": 1, "summary": summary, "providers": results}
    (staging_root / "test-report.json").write_text(_json_text(output), encoding="utf-8")
    return output


def promote_staging(plugin_root: Path | str, staging_root: Path | str, only_passed: bool = True,
                    backup_root: Path | str | None = None) -> dict[str, Any]:
    """Promote passed staged plugins, retaining a recoverable backup."""
    plugin_root = Path(plugin_root).resolve()
    staging_root = Path(staging_root).resolve()
    report_path = staging_root / "test-report.json"
    if not report_path.is_file():
        raise RuntimeError("run the staging test command before promotion")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    backup_root = Path(backup_root or (staging_root / "backups")).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    promoted: list[str] = []
    skipped: list[dict[str, str]] = []
    for result in report.get("providers", []):
        provider_id = result["provider_id"]
        if only_passed and result.get("status") != "passed":
            skipped.append({"provider_id": provider_id, "reason": result.get("status", "unknown")})
            continue
        source = staging_root / provider_id
        target = plugin_root / provider_id
        if not source.is_dir():
            skipped.append({"provider_id": provider_id, "reason": "staged directory missing"})
            continue
        if target.exists():
            backup = backup_root / stamp / provider_id
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(target, backup)
            shutil.rmtree(target)
        shutil.copytree(source, target)
        promoted.append(provider_id)
    output = {"promoted": promoted, "skipped": skipped, "backup_root": str(backup_root)}
    (staging_root / "promotion-report.json").write_text(_json_text(output), encoding="utf-8")
    return output


def _safe_live_download(item: Any, max_bytes: int) -> dict[str, Any]:
    """Verify a resolved URL without returning or logging the URL itself."""
    direct_url = getattr(item, "direct_url", None)
    if not isinstance(direct_url, str) or not direct_url.startswith(("http://", "https://")):
        raise ValueError("provider returned no safe HTTP download URL")
    request = urllib.request.Request(direct_url, headers=dict(getattr(item, "headers", {}) or {}))
    total = 0
    with urllib.request.urlopen(request, timeout=30) as response:
        status = int(getattr(response, "status", 200) or 200)
        while True:
            chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"live probe exceeded max bytes ({max_bytes})")
    return {"status_code": status, "bytes_read": total}


def _probe_live_provider(registry: Any, provider_id: str, url: str, download: bool,
                         max_bytes: int) -> dict[str, Any]:
    """Run a configured, user-authorized provider probe with secret-free output."""
    if registry.provider_for(url) != provider_id:
        raise ValueError("URL routed to a different provider")
    inspection = registry.inspect_url(url)
    is_tree = bool(inspection["capabilities"].get("folders") and inspection["hooks"].get("enumerate"))
    items = registry.enumerate(url) if is_tree else registry.resolve(url)
    if not items:
        raise ValueError("provider returned no items")
    files = [item for item in items if (item.metadata or {}).get("type", "file") == "file"]
    if not files:
        raise ValueError("provider returned no downloadable file items")
    resolved = registry.resolve(url, {"selected_item_ids": [files[0].item_id]}) if is_tree else files
    if not resolved or not all(getattr(item, "direct_url", None) for item in resolved):
        raise ValueError("provider did not resolve a direct URL")
    result: dict[str, Any] = {
        "status": "passed",
        "ui_mode": inspection["ui_mode"],
        "enumerated_items": len(items),
        "resolved_items": len(resolved),
        "sample_names": [str(item.display_name or "")[:120] for item in resolved[:3]],
    }
    if download:
        result["download"] = _safe_live_download(resolved[0], max_bytes)
    return result


def _har_status(root: Path, provider_id: str) -> dict[str, Any] | None:
    names = [provider_id]
    if provider_id == "vikingfile":
        names.append("viking")
    candidates = [candidate for name in names for candidate in (
        root / "hars" / f"{name}.har", root / f"{name}.har"
    )]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        entries = value.get("log", {}).get("entries", []) if isinstance(value, dict) else []
        if not isinstance(entries, list):
            raise ValueError("HAR entries must be an array")
        return {"status": "parsed", "file": str(path.relative_to(root)).replace("\\", "/"),
                "entries": len(entries)}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "invalid", "file": str(path.relative_to(root)).replace("\\", "/"),
                "error": type(exc).__name__}


def check_active_plugins(plugin_root: Path | str, cyberdrop_root: Path | str, *, live: bool = False,
                         download: bool = False, max_bytes: int = 10 * 1024 * 1024) -> dict[str, Any]:
    """Check active provider contracts and optionally configured live URLs.

    Offline checks never contact provider sites.  Live checks are explicitly
    opt-in and read URLs from TRANSFER_MANAGER_LIVE_URL_<PROVIDER> variables.
    Reports contain counts, statuses, and safe filenames only; URLs and raw
    provider errors are intentionally excluded.
    """
    from .plugin_contracts import validate_plugin_contract
    from .plugins import PluginRegistry

    root = Path(plugin_root).resolve()
    reference_root = Path(cyberdrop_root).resolve()
    repo_root = Path(__file__).resolve().parents[1]
    records = compare_plugin_ecosystems(root, reference_root)
    specs = {spec.provider_id: spec for spec in build_specs(root, reference_root)}
    registry = PluginRegistry() if root == (Path(__file__).resolve().parents[1] / "plugins").resolve() else PluginRegistry(external_dirs=[root])
    providers: list[dict[str, Any]] = []
    try:
        for record in records:
            entry: dict[str, Any] = {"provider_id": record.provider_id, "status": "passed",
                                     "reference_fixture": bool(record.reference_fixture),
                                     "fixture_cases": len(specs.get(record.provider_id).fixture_cases) if record.provider_id in specs else 0}
            try:
                validate_plugin_contract(root / record.provider_id)
                routing_is_synthetic = record.routing_fixture_url == "http://fixture.example.invalid/fixture"
                if (record.routing_fixture_url and not routing_is_synthetic
                        and registry.provider_for(record.routing_fixture_url) != record.provider_id):
                    raise AssertionError("routing fixture selected a different provider")
                entry["contract"] = "passed"
            except Exception as exc:
                entry["status"] = "failed"
                entry["contract"] = "failed"
                entry["error_type"] = type(exc).__name__
            live_url = os.environ.get(record.live_url_env) if live else None
            if live and not live_url:
                entry["live"] = "not_configured"
            elif live and live_url and entry["status"] == "passed":
                try:
                    entry["live"] = _probe_live_provider(registry, record.provider_id, live_url, download, max_bytes)
                except Exception as exc:
                    entry["live"] = {"status": "failed", "error_type": type(exc).__name__}
                    entry["status"] = "failed"
            elif live:
                entry["live"] = "not_run_contract_failed"
            providers.append(entry)
    finally:
        registry.close()
    har_fixtures = [status for provider_id in ("mediafire", "mega", "gofile", "pixeldrain", "vikingfile")
                    if (status := _har_status(repo_root, provider_id)) is not None]
    summary = {
        "providers": len(providers),
        "passed": sum(item["status"] == "passed" for item in providers),
        "failed": sum(item["status"] == "failed" for item in providers),
        "live_configured": sum(isinstance(item.get("live"), dict) and item["live"].get("status") == "passed" for item in providers),
        "live_not_configured": sum(item.get("live") == "not_configured" for item in providers),
        "har_fixtures": len(har_fixtures),
    }
    return {"schema": 1, "mode": "live" if live else "offline", "summary": summary,
            "providers": providers, "har_fixtures": har_fixtures}
