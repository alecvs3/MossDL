from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from .errors import NeedsUser
from .errors import ProviderMappedError
from .plugin_catalog import load_manifest
from .provider_sdk import ProviderContractError, declared_operations, validate_operation_envelope
from .recipe_runtime import RecipeRuntime


def _load_hook(plugin_dir: Path, name: str):
    sys.path.insert(0, str(plugin_dir))
    try:
        module = importlib.import_module("hooks")
        return getattr(module, name)
    finally:
        if sys.path and sys.path[0] == str(plugin_dir):
            sys.path.pop(0)


def _install_telemetry_bridge() -> None:
    """Forward provider telemetry to the supervising engine.

    Plugin workers run in an isolated subprocess, so their telemetry bus is a
    different object than the engine's. Emit each record as a marked JSON line
    on stderr; ``PluginProcess`` re-emits it through the engine telemetry bus so
    provider decisions appear in the run log instead of being lost.
    """
    try:
        from .telemetry import telemetry_bus
    except Exception:
        return

    def _bridge(event: Any) -> None:
        try:
            payload = {
                "level": getattr(event, "level", "INFO"),
                "subsystem": getattr(event, "subsystem", ""),
                "message": getattr(event, "message", ""),
                "context": getattr(event, "context", None),
            }
            sys.stderr.write("@@PLUGIN_TELEMETRY@@" + json.dumps(payload, default=str) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    try:
        telemetry_bus.add_listener(_bridge)
    except Exception:
        pass
    try:
        from . import critical_trace

        def _critical_bridge(row: dict[str, Any]) -> None:
            sys.stderr.write("@@PLUGIN_CRITICAL@@" + json.dumps(row, default=str) + "\n")
            sys.stderr.flush()

        critical_trace.set_external_sink(_critical_bridge)
    except Exception:
        pass


def _seed_clearance(clearance: dict[str, Any]) -> None:
    """Install engine-supplied clearance cookies into this worker's cache.

    Re-seeding on every call would emit a [CLEARANCE_CACHED] record per request,
    so this only writes when the worker has no valid entry for the domain.
    """
    from .http_client import clearance_cache

    domain = str(clearance.get("domain") or "")
    cookies = dict(clearance.get("cookies") or {})
    # An empty-cookie entry is worse than a cold cache: requests would report
    # clearance_hit=True while sending no clearance at all.
    if not domain or not cookies or clearance_cache.get_clearance(domain) is not None:
        return
    ttl = float(clearance.get("ttl_remaining") or 0.0)
    if ttl <= 0:
        return
    clearance_cache.set_clearance(
        domain,
        cookies,
        str(clearance.get("user_agent") or ""),
        ttl=ttl,
    )


def main() -> int:
    _install_telemetry_bridge()
    args = sys.argv[1:]
    plugin_dir = Path(args[args.index("--plugin-dir") + 1]).resolve()
    manifest = load_manifest(plugin_dir)
    recipe_runtime = RecipeRuntime(plugin_dir, manifest) if manifest.get("implementation") == "recipe" else None
    for line in sys.stdin:
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            method = request.get("method")
            params = request.get("params") or {}
            if not isinstance(method, str):
                raise ProviderContractError("provider method must be a string")
            if isinstance(params, dict) and any(str(key).lower() in {
                "db", "database", "engine", "engine_state", "lifecycle", "task_state",
                "admission", "resource_manager", "scheduler", "lease", "queue",
            } for key in params):
                raise ProviderContractError("provider requests cannot carry engine authority")
            trace_context = params.pop("_critical_trace", None) if isinstance(params, dict) else None
            # Replant the engine's anti-bot clearance into this process's cache
            # before dispatching. The cache is per-process, so without this every
            # provider request starts cold (clearance_hit=False) and re-challenges
            # even when the engine solved the same host seconds earlier. Seeding
            # the cache rather than the provider means every provider benefits
            # without touching provider code. The key is removed so the hook sees
            # its declared params only.
            route = params.pop("_route", None) if isinstance(params, dict) else None
            if isinstance(route, dict):
                from . import route_http
                route_http.bind_task_route(route.get("proxy"))
            clearance = params.pop("clearance", None) if isinstance(params, dict) else None
            if isinstance(clearance, dict) and clearance.get("cookies"):
                _seed_clearance(clearance)
            if method == "manifest":
                result: Any = manifest
            elif method == "match":
                from urllib.parse import urlsplit
                parsed = urlsplit(params["url"])
                match = manifest["match"]
                hostname = (parsed.hostname or "").lower()
                host_patterns = manifest.get("hosts", [])
                def _host_matches(h: str, pat: str) -> bool:
                    pat = pat.lower()
                    if pat == "*" or h == pat or h.endswith("." + pat):
                        return True
                    return __import__("fnmatch").fnmatch(h, pat)

                result = (
                    (not match.get("schemes") or parsed.scheme in match["schemes"])
                    and (not host_patterns or any(_host_matches(hostname, pattern) for pattern in host_patterns))
                    and (not match.get("path_regex") or re.search(match["path_regex"], parsed.path) is not None)
                )
            elif method in {"resolve", "refresh", "authenticate", "metadata", "enumerate", "health", "extract", "extract_links", "helper"}:
                declared = declared_operations(manifest)
                if method not in declared:
                    raise ProviderContractError(f"provider operation is not declared: {method}")
                if recipe_runtime is not None:
                    if isinstance(trace_context, dict):
                        from .critical_trace import bind
                        with bind(**trace_context):
                            result = recipe_runtime.call(method, params)
                    else:
                        result = recipe_runtime.call(method, params)
                else:
                    hook_name = manifest.get("hooks", {}).get(method)
                    if not hook_name:
                        raise ValueError(f"plugin does not implement {method}")
                    if isinstance(trace_context, dict):
                        from .critical_trace import bind
                        with bind(**trace_context):
                            result = _load_hook(plugin_dir, hook_name)(params)
                    else:
                        result = _load_hook(plugin_dir, hook_name)(params)
                result = validate_operation_envelope(manifest["id"], method, result, declared=declared)
            else:
                raise ValueError(f"unknown method: {method}")
            response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
        except NeedsUser as exc:
            response = {"jsonrpc": "2.0", "id": request.get("id"), "error": {
                "type": "NeedsUser", "message": str(exc), "action": exc.action, "challenge": exc.challenge}}
        except ProviderMappedError as exc:
            response = {"jsonrpc": "2.0", "id": request.get("id"), "error": {
                "type": "ProviderMappedError", "message": str(exc), "category": exc.category,
                "status_code": exc.status_code, "retry_after": exc.retry_after}}
        except Exception as exc:
            err_dict = {"type": type(exc).__name__, "message": str(exc)}
            if type(exc).__name__ == "NeedsUser":
                err_dict["action"] = getattr(exc, "action", "user_action")
                err_dict["challenge"] = getattr(exc, "challenge", None)
            response = {"jsonrpc": "2.0", "id": request.get("id"), "error": err_dict}
        print(json.dumps(response), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
