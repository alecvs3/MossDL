from __future__ import annotations

import fnmatch
import json
import os
import logging
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .errors import EngineError, NeedsCaptcha, NeedsUser, ProviderMappedError
from .models import ResolvedItem
from .plugin_catalog import ManifestError, load_manifest
from .provider_sdk import (
    ProviderContractError,
    ProviderOperationRequest,
    ProviderOperationResult,
    declared_operations,
    validate_operation_envelope,
)
from .shortlinks import fetch_and_extract, looks_like_html_item

logger = logging.getLogger("engine.plugins")


class PluginProcess:
    """Persistent isolated JSON-RPC worker for one validated plugin."""

    def __init__(self, plugin_dir: Path | str, timeout: float = 120.0) -> None:
        plugin_dir = Path(plugin_dir)
        if not plugin_dir.is_dir():
            plugin_dir = Path(__file__).resolve().parents[1] / "plugins" / str(plugin_dir)
        self.plugin_dir = plugin_dir.resolve()
        self.provider_id = self.plugin_dir.name
        self.timeout = timeout
        self.process = subprocess.Popen(
            [sys.executable, "-m", "engine.plugin_worker", "--plugin-dir", str(plugin_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=str(Path(__file__).resolve().parent.parent),
        )
        self._start_stderr_drain()
        self._id = 0
        self._lock = threading.RLock()

    def _start_stderr_drain(self) -> None:
        if self.process and self.process.stderr:
            proc_stderr = self.process.stderr
            pid = self.provider_id

            def _drain() -> None:
                try:
                    for line in iter(proc_stderr.readline, ""):
                        if not line:
                            break
                        stripped = line.rstrip()
                        if stripped.startswith("@@PLUGIN_TELEMETRY@@"):
                            try:
                                payload = json.loads(stripped[len("@@PLUGIN_TELEMETRY@@"):])
                                from .telemetry import telemetry_bus
                                telemetry_bus.record(
                                    level=str(payload.get("level") or "INFO"),
                                    subsystem=f"plugin:{pid}:{payload.get('subsystem') or 'provider'}",
                                    message=str(payload.get("message") or ""),
                                    context=payload.get("context") if isinstance(payload.get("context"), dict) else None,
                                )
                            except Exception:
                                pass
                            continue
                        if stripped.startswith("@@PLUGIN_CRITICAL@@"):
                            try:
                                from . import critical_trace
                                payload = json.loads(stripped[len("@@PLUGIN_CRITICAL@@"):])
                                if isinstance(payload, dict):
                                    critical_trace.ingest_external(payload)
                            except Exception:
                                pass
                            continue
                        if any(err_kw in stripped for err_kw in ("Traceback", "Error", "Exception")):
                            logger.warning("[PLUGIN_WORKER %s] %s", pid, stripped)
                        else:
                            logger.debug("[PLUGIN_WORKER %s] %s", pid, stripped)
                except Exception:
                    pass

            threading.Thread(target=_drain, daemon=True).start()

    def _ensure_process(self) -> None:
        if self.process is None or self.process.poll() is not None or not self.process.stdin or not self.process.stdout:
            try:
                if self.process:
                    self.process.kill()
            except Exception:
                pass
            self.process = subprocess.Popen(
                [sys.executable, "-m", "engine.plugin_worker", "--plugin-dir", str(self.plugin_dir)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", cwd=str(Path(__file__).resolve().parent.parent),
            )
            self._start_stderr_drain()
            self._id = 0

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        eff_timeout = timeout if timeout is not None else self.timeout
        with self._lock:
            self._ensure_process()
            if self.process.poll() is not None or not self.process.stdin or not self.process.stdout:
                raise EngineError(f"plugin {self.provider_id} worker is unavailable")
            self._id += 1
            call_params = dict(params or {})
            # The worker is another process and never sees the engine's route
            # state; every call carries the route it must use.
            from . import route_http
            call_params["_route"] = {"proxy": route_http.active_proxy()}
            from . import critical_trace
            if critical_trace.active():
                call_params["_critical_trace"] = critical_trace.identity_payload()
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": call_params}) + "\n")
            self.process.stdin.flush()
            result: queue.Queue[str] = queue.Queue(maxsize=1)
            threading.Thread(target=lambda: result.put(self.process.stdout.readline()), daemon=True).start()
            try:
                line = result.get(timeout=eff_timeout)
            except queue.Empty as exc:
                self.close()
                raise EngineError(f"plugin {self.provider_id} timed out") from exc
            if not line:
                raise EngineError(f"plugin {self.provider_id} exited unexpectedly")
            if len(line) > 4 * 1024 * 1024:
                self.close()
                raise EngineError(f"plugin {self.provider_id} response exceeded the output limit")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                failure = EngineError(f"plugin {self.provider_id} returned malformed JSON")
                failure.plugin_fault = True
                raise failure from exc
            if not isinstance(response, dict):
                failure = EngineError(f"plugin {self.provider_id} returned an invalid response")
                failure.plugin_fault = True
                raise failure
            if "error" in response:
                error = response["error"]
                if error.get("type") == "NeedsCaptcha" or (error.get("type") == "NeedsUser" and error.get("action") == "captcha"):
                    c = error.get("challenge") or {}
                    raise NeedsCaptcha(
                        error.get("message", "captcha required"),
                        captcha_type=c.get("captcha_type", "image_text"),
                        params=c.get("params", {}),
                        timeout=float(c.get("timeout_seconds", 90.0)),
                        challenge_id=c.get("challenge_id"),
                    )
                if error.get("type") == "NeedsUser":
                    raise NeedsUser(error.get("message", "user action required"), error.get("action", "user_action"), error.get("challenge"))
                if error.get("type") == "ProviderMappedError":
                    raise ProviderMappedError(error.get("message", "provider failed"), error.get("category", "unknown"),
                                              error.get("status_code"), error.get("retry_after"))
                failure = EngineError(error.get("message", "plugin failed"))
                # Provider-side availability/auth/quota responses are normal
                # outcomes and must not quarantine a healthy plugin process.
                failure.plugin_fault = error.get("type") not in {
                    "ProviderUnavailable", "HTTPError", "URLError", "TimeoutError", "ConnectionError"
                }
                raise failure
            return response.get("result")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass


class PluginProcessPool:
    """Pool of up to max_workers persistent isolated JSON-RPC workers for one provider."""

    def __init__(self, plugin_dir: Path | str, max_workers: int = 3, timeout: float = 120.0) -> None:
        self.plugin_dir = Path(plugin_dir)
        self.max_workers = max(1, int(max_workers))
        self.timeout = timeout
        self._available: queue.Queue[PluginProcess] = queue.Queue()
        self._all_processes: list[PluginProcess] = []
        self._lock = threading.Lock()
        self._closed = False

    def acquire(self, timeout: float | None = None) -> PluginProcess:
        eff_timeout = timeout if timeout is not None else self.timeout
        with self._lock:
            if self._closed:
                raise EngineError(f"Plugin pool for {self.plugin_dir.name} is closed")
            while not self._available.empty():
                try:
                    proc = self._available.get_nowait()
                    if proc.process.poll() is None:
                        return proc
                    proc.close()
                    if proc in self._all_processes:
                        self._all_processes.remove(proc)
                except queue.Empty:
                    break

            if len(self._all_processes) < self.max_workers:
                proc = PluginProcess(self.plugin_dir, timeout=self.timeout)
                self._all_processes.append(proc)
                return proc

        try:
            proc = self._available.get(timeout=eff_timeout)
            if proc.process.poll() is not None:
                with self._lock:
                    proc.close()
                    if proc in self._all_processes:
                        self._all_processes.remove(proc)
                    proc = PluginProcess(self.plugin_dir, timeout=self.timeout)
                    self._all_processes.append(proc)
            return proc
        except queue.Empty as exc:
            raise EngineError(f"Timed out waiting for an available plugin worker for {self.plugin_dir.name}") from exc

    def release(self, proc: PluginProcess) -> None:
        with self._lock:
            if self._closed:
                proc.close()
                return
            if proc.process.poll() is None:
                self._available.put(proc)
            else:
                proc.close()
                if proc in self._all_processes:
                    self._all_processes.remove(proc)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for proc in list(self._all_processes):
                try:
                    proc.close()
                except Exception:
                    pass
            self._all_processes.clear()
            while not self._available.empty():
                try:
                    self._available.get_nowait()
                except queue.Empty:
                    break


def _matches_manifest(manifest: dict[str, Any], url: str) -> bool:
    import fnmatch
    import urllib.parse
    parsed = urllib.parse.urlsplit(url)
    match = manifest.get("match", {})
    hostname = (parsed.hostname or "").lower()
    host_patterns = manifest.get("hosts", [])
    if match.get("schemes") and parsed.scheme not in match["schemes"]:
        return False
    if host_patterns:
        matched_host = False
        for pat in host_patterns:
            pat_lower = pat.lower()
            if pat_lower == "*" or hostname == pat_lower or hostname.endswith("." + pat_lower) or fnmatch.fnmatch(hostname, pat_lower):
                matched_host = True
                break
        if not matched_host:
            return False
    if match.get("path_regex") and re.search(match["path_regex"], parsed.path) is None:
        return False
    return True


class PluginRegistry:
    def __init__(self, external_dirs: list[str | Path] | None = None, health_store: Any | None = None) -> None:
        root = Path(__file__).resolve().parents[1] / "plugins"
        roots = [root]
        configured = external_dirs or [item for item in os.environ.get("TRANSFER_PLUGIN_DIRS", "").split(os.pathsep) if item]
        roots.extend(Path(item).resolve() for item in configured)
        self.plugin_dirs: dict[str, Path] = {}
        self.manifests: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self._processes: dict[str, PluginProcess] = {}
        self._pools: dict[str, PluginProcessPool] = {}
        self.health_store = health_store
        self._resource_manager: Any | None = None
        self.health: dict[str, dict[str, Any]] = {}
        for base in roots:
            if not base.is_dir():
                continue
            for directory in sorted(base.iterdir()):
                if not directory.is_dir() or directory.name in self.plugin_dirs:
                    continue
                try:
                    self.manifests[directory.name] = load_manifest(directory)
                    self.plugin_dirs[directory.name] = directory
                    self.order.append(directory.name)
                except (ManifestError, OSError):
                    continue
        for provider_id in self.order:
            persisted = next((row for row in (health_store.list_plugin_health() if health_store else [])
                              if row["plugin_id"] == provider_id), None)
            self.health[provider_id] = persisted or {
                "plugin_id": provider_id, "version": self.manifests[provider_id].get("version", ""),
                "enabled": True, "quarantined": False, "failure_count": 0, "last_error": None,
            }
            if self.health[provider_id].get("quarantined"):
                updated_at = float(self.health[provider_id].get("updated_at") or 0)
                if updated_at and (time.time() - updated_at > 300.0):
                    # Startup auto-healing: unquarantine on engine boot if 5m probation has elapsed
                    self.health[provider_id]["quarantined"] = False
                    self.health[provider_id]["failure_count"] = 0
                    self._persist_health(provider_id)
            elif health_store and not persisted:
                self._persist_health(provider_id)

    def is_quarantined(self, provider_id: str) -> bool:
        health = self.health.get(provider_id, {})
        if not health.get("quarantined", False):
            return False
        # Probation TTL: auto-lift quarantine after 5 minutes so temporary faults recover
        updated_at = float(health.get("updated_at") or 0)
        if updated_at and (time.time() - updated_at > 300.0):
            health["quarantined"] = False
            health["failure_count"] = 0
            self._persist_health(provider_id)
            return False
        return True

    def _persist_health(self, provider_id: str) -> None:
        if self.health_store:
            value = self.health[provider_id]
            persisted = {key: item for key, item in value.items() if key not in {"plugin_id", "version"}}
            self.health_store.save_plugin_health(provider_id, value.get("version", ""), **persisted)

    def set_resource_manager(self, manager: Any) -> None:
        """Attach the engine-wide adaptive admission controller."""
        self._resource_manager = manager

    @staticmethod
    def _clearance_for(params: dict[str, Any] | None) -> dict[str, Any] | None:
        """Snapshot cached anti-bot clearance for the URL this call targets.

        Providers run in a separate process with their own (empty)
        ``clearance_cache``, so clearance won a solve ago in the engine never
        reached them: every sibling of a multipart package re-challenged even
        though valid cookies were sitting in the engine's cache. The snapshot
        travels with the request and is replanted in the worker.
        """
        from .http_client import clearance_cache

        url = str((params or {}).get("url") or (params or {}).get("input_url") or "")
        if not url:
            item = (params or {}).get("item")
            if isinstance(item, dict):
                url = str(item.get("source_url") or item.get("direct_url") or "")
        if not url:
            return None
        entry = clearance_cache.get_clearance(url)
        if not entry:
            return None
        remaining = entry.ttl_seconds - (time.time() - entry.created_at)
        if remaining <= 0:
            return None
        return {
            "domain": entry.hostname,
            "cookies": dict(entry.cookies),
            "user_agent": entry.user_agent,
            "ttl_remaining": remaining,
        }

    def _call(self, provider_id: str, method: str, params: dict[str, Any] | None = None,
              timeout: float | None = None) -> Any:
        state = self.health.get(provider_id, {})
        if not state.get("enabled", True) or self.is_quarantined(provider_id):
            raise EngineError(f"provider {provider_id} is unavailable")
        clearance = self._clearance_for(params)
        if clearance is not None:
            params = {**(params or {}), "clearance": clearance}
        permit = None
        if self._resource_manager is not None:
            manifest_limits = self.manifests.get(provider_id, {}).get("limits", {}) or {}
            url = str((params or {}).get("url") or (params or {}).get("input_url") or "")
            host = (urllib.parse.urlsplit(url).hostname or provider_id).lower()
            account_raw = (params or {}).get("account")
            account = str((params or {}).get("account_ref") or (account_raw.get("id", "-") if isinstance(account_raw, dict) else "-"))
            permit = self._resource_manager.adaptive.acquire(
                f"{provider_id}:{account}:{host}",
                ceiling=int(manifest_limits.get("max_concurrent_items", self._resource_manager.policy.per_provider_transfers)),
                requests_per_second=float(manifest_limits.get("requests_per_second", 0) or 0),
            )
        pool = self._pool(provider_id)
        proc = pool.acquire(timeout=timeout)
        try:
            result = proc.call(method, params, timeout=timeout)
            if permit:
                permit.finish(True)
        except NeedsUser:
            if permit:
                permit.finish(False)
            raise
        except ProviderMappedError as exc:
            # A classified provider response (expired URL, quota, not-found,
            # etc.) is an expected runtime outcome, not an implementation
            # fault. Let the engine decide whether to refresh, retry, or fail
            # the task without quarantining the provider.
            if permit:
                permit.finish(False, status_code=getattr(exc, "status_code", None),
                              retry_after=getattr(exc, "retry_after", None))
            raise
        except EngineError as exc:
            if permit:
                permit.finish(False, status_code=getattr(exc, "status_code", None),
                              retry_after=getattr(exc, "retry_after", None))
            if not getattr(exc, "plugin_fault", True):
                raise
            value = self.health[provider_id]
            value["failure_count"] = int(value.get("failure_count", 0)) + 1
            value["last_error"] = str(exc)[:500]
            if value["failure_count"] >= 3:
                value["quarantined"] = True
            self._persist_health(provider_id)
            raise
        except Exception as exc:
            if permit:
                permit.finish(False, status_code=getattr(exc, "status_code", None),
                              retry_after=getattr(exc, "retry_after", None))
            raise
        finally:
            pool.release(proc)
        value = self.health[provider_id]
        if value.get("failure_count") or value.get("last_error") or value.get("quarantined"):
            value["failure_count"], value["last_error"], value["quarantined"] = 0, None, False
            self._persist_health(provider_id)
        return result

    def list_health(self) -> list[dict[str, Any]]:
        return [{"plugin_id": plugin_id, **value} for plugin_id, value in sorted(self.health.items())]

    def set_state(self, provider_id: str, enabled: bool | None = None,
                  quarantined: bool | None = None) -> dict[str, Any]:
        if provider_id not in self.health:
            raise KeyError("plugin not found")
        value = self.health[provider_id]
        if enabled is not None:
            value["enabled"] = bool(enabled)
        if quarantined is not None:
            value["quarantined"] = bool(quarantined)
            if not quarantined:
                value["failure_count"], value["last_error"] = 0, None
        self._persist_health(provider_id)
        return {"plugin_id": provider_id, **value}

    def manifests_with_health(self) -> list[dict[str, Any]]:
        return [{**self.manifests[plugin_id], "health": self.health.get(plugin_id, {})}
                for plugin_id in self.order]

    def provider_catalog(self) -> list[dict[str, Any]]:
        """Return safe provider presentation metadata for UI consumers."""
        return [{
            "id": provider_id,
            "display_name": self.manifests[provider_id].get("display_name") or provider_id,
            "icon": self.manifests[provider_id].get("icon"),
            "hosts": list(self.manifests[provider_id].get("hosts", [])),
            "enabled": bool(self.health.get(provider_id, {}).get("enabled", True)),
            # Appended for the Plugins page; earlier consumers ignore them.
            "capabilities": sorted(key for key, on in (self.manifests[provider_id].get("capabilities") or {}).items() if on),
            "roles": list(self.manifests[provider_id].get("roles") or []),
            "category": self.manifests[provider_id].get("category"),
        } for provider_id in self.order]

    def has_operation(self, provider_id: str, operation: str) -> bool:
        manifest = self.manifests.get(provider_id, {})
        if operation in declared_operations(manifest):
            return True
        if manifest.get("implementation") != "recipe":
            return False
        try:
            from .recipe_runtime import load_recipe
            recipe = load_recipe(self.plugin_dirs[provider_id], manifest)
            return operation in recipe.get("operations", {})
        except Exception:
            return False

    def call_capability(self, provider_id: str, operation: str,
                        params: dict[str, Any] | None = None) -> ProviderOperationResult:
        """Invoke a manifest-declared provider capability through the worker.

        The returned envelope is engine-owned; provider output never selects a
        task state, writes persistence, or acquires admission on its own.
        Legacy resolve/enumerate callers continue to use their list payloads.
        """
        if provider_id not in self.manifests:
            raise KeyError("plugin not found")
        if not self.health[provider_id].get("enabled", True) or self.is_quarantined(provider_id):
            raise EngineError(f"provider {provider_id} is unavailable")
        manifest = self.manifests[provider_id]
        if operation not in declared_operations(manifest):
            raise ProviderContractError(f"provider operation is not declared: {operation}")
        request = ProviderOperationRequest(provider_id, operation, params or {})
        value = self._call(provider_id, operation, request.params)
        validate_operation_envelope(provider_id, operation, value, declared=declared_operations(manifest))
        return ProviderOperationResult(provider_id, operation, value)

    def _pool(self, provider_id: str) -> PluginProcessPool:
        pool = self._pools.get(provider_id)
        if pool is None:
            pool = PluginProcessPool(self.plugin_dirs[provider_id], max_workers=3, timeout=120.0)
            self._pools[provider_id] = pool
        return pool

    def _process(self, provider_id: str) -> PluginProcess:
        process = self._processes.get(provider_id)
        if process is None or process.process.poll() is not None:
            if process:
                process.close()
            process = PluginProcess(self.plugin_dirs[provider_id])
            self._processes[provider_id] = process
        return process

    def provider_for(self, url: str) -> str:
        resolver_ids = [provider_id for provider_id in self.order
                        if "resolver" in self.manifests[provider_id].get("roles", ["resolver"])
                        and self.health[provider_id].get("enabled", True)
                        and not self.is_quarantined(provider_id)]
        ordered = sorted(resolver_ids, key=lambda provider_id: (self.manifests[provider_id].get("hosts") == ["*"], provider_id))
        for provider_id in ordered:
            if self.manifests[provider_id].get("hosts") == ["*"]:
                continue
            if not _matches_manifest(self.manifests[provider_id], url):
                continue
            if self.manifests[provider_id].get("hooks", {}).get("match"):
                try:
                    if not self._call(provider_id, "match", {"url": url}):
                        continue
                except EngineError:
                    continue
            return provider_id

        # Finally check catch-all resolvers (generic, media, etc.)
        for provider_id in ordered:
            if self.manifests[provider_id].get("hosts") == ["*"]:
                if not _matches_manifest(self.manifests[provider_id], url):
                    continue
                if self.manifests[provider_id].get("hooks", {}).get("match"):
                    try:
                        if not self._call(provider_id, "match", {"url": url}):
                            continue
                    except EngineError:
                        continue
                return provider_id
        raise EngineError("no installed provider recognizes this URL")

    def detect_provider(self, url: str) -> str | None:
        try:
            return self.provider_for(url)
        except Exception:
            return None

    def inspect_url(self, url: str) -> dict[str, Any]:
        provider_id = self.provider_for(url)
        manifest = self.manifests[provider_id]
        capabilities = manifest.get("capabilities", {})
        if isinstance(capabilities, list):
            capabilities = {str(value): True for value in capabilities}
        capabilities = {str(key): value for key, value in capabilities.items()}
        has_enumerate = self.has_operation(provider_id, "enumerate")
        has_resolve = self.has_operation(provider_id, "resolve")
        tree_picker = bool(capabilities.get("folders") and has_enumerate)
        return {
            "provider_id": provider_id,
            "display_name": manifest.get("display_name") or provider_id,
            "capabilities": {**capabilities, "enumerate": has_enumerate, "resolve": has_resolve},
            "hooks": {operation: self.has_operation(provider_id, operation)
                      for operation in ("metadata", "enumerate", "resolve", "refresh", "authenticate")},
            "ui_mode": "tree_picker" if tree_picker else "download",
        }

    def resolve(self, url: str, secrets: dict[str, str] | None = None,
                timeout: float | None = None) -> list[ResolvedItem]:
        provider_id = self.provider_for(url)
        result = self._call(provider_id, "resolve", {"url": url, "secrets": secrets or {}}, timeout=timeout)
        return [ResolvedItem(**item) for item in result]

    def refresh(self, item: ResolvedItem, reason: str, secrets: dict[str, str] | None = None,
                status_code: int | None = None) -> list[ResolvedItem]:
        provider_id = item.provider
        if provider_id not in self.plugin_dirs or not self.has_operation(provider_id, "refresh"):
            return []
        result = self._call(provider_id, "refresh", {"item": item.to_dict(), "reason": reason,
                                                       "status_code": status_code, "secrets": secrets or {}})
        return [ResolvedItem(**value) for value in result]

    def enumerate(self, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        provider_id = self.provider_for(url)
        if not self.has_operation(provider_id, "enumerate"):
            return self.resolve(url, secrets)
        try:
            result = self._call(provider_id, "enumerate", {"url": url, "secrets": secrets or {}})
            if result:
                return [ResolvedItem(**value) for value in result]
        except Exception as err:
            if self.has_operation(provider_id, "resolve"):
                try:
                    resolved = self.resolve(url, secrets)
                    if resolved:
                        return resolved
                except Exception:
                    pass
            raise err
        if self.has_operation(provider_id, "resolve"):
            try:
                resolved = self.resolve(url, secrets)
                if resolved:
                    return resolved
            except Exception:
                pass
        return []

    def extract_links(self, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        for provider_id in sorted(self.order, key=lambda value: (value != "shortlink", value)):
            manifest = self.manifests[provider_id]
            if "decrypter" not in manifest.get("roles", []) or not self.has_operation(provider_id, "extract_links"):
                continue
            if not self.health[provider_id].get("enabled", True) or self.is_quarantined(provider_id):
                continue
            if self._call(provider_id, "match", {"url": url}):
                result = self._call(provider_id, "extract_links", {"url": url, "secrets": secrets or {}})
                if result:
                    return [ResolvedItem(**value) for value in result]
        return []

    def shortlink_extract(self, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        if "shortlink" not in self.plugin_dirs or not self.health["shortlink"].get("enabled", True):
            return []
        result = self._call("shortlink", "extract_links", {"url": url, "secrets": secrets or {}})
        return [ResolvedItem(**value) for value in result]

    def resolve_chain(self, url: str, secrets: dict[str, str] | None = None, max_hops: int = 8,
                      on_hop: Any | None = None) -> list[ResolvedItem]:
        current = url
        visited = {current}
        traversed_shortlink = False
        for hop in range(max_hops):
            if on_hop:
                on_hop(hop, current, "shortlink")
            extracted = self.shortlink_extract(current, secrets)
            if not extracted:
                if on_hop:
                    on_hop(hop, current, "provider")
                resolved = self.resolve(current, secrets)
                # Generic HTTP resolution follows redirects for metadata, but
                # the final URL may belong to a first-party provider or be an
                # HTML download page. Re-enter the provider chain so a t.co,
                # bit.ly, or similar redirect is not treated as the file.
                redirect_target = None
                if len(resolved) == 1 and resolved[0].provider == "generic":
                    metadata = resolved[0].metadata or {}
                    candidate = metadata.get("final_url")
                    if metadata.get("redirected") and isinstance(candidate, str) and candidate:
                        redirect_target = candidate
                if redirect_target and redirect_target not in visited:
                    try:
                        target_provider = self.provider_for(redirect_target)
                    except EngineError:
                        target_provider = "generic"
                    if target_provider != "generic":
                        visited.add(redirect_target)
                        current = redirect_target
                        traversed_shortlink = True
                        continue
                    if resolved and all(looks_like_html_item(item) for item in resolved):
                        try:
                            candidates = fetch_and_extract(redirect_target)
                        except Exception:
                            candidates = []
                        candidates = [candidate for candidate in candidates if candidate not in visited]
                        if len(candidates) == 1:
                            visited.add(candidates[0])
                            current = candidates[0]
                            traversed_shortlink = True
                            continue
                        raise NeedsUser(
                            "Redirect ended on an HTML download page that needs a selectable link or browser interaction",
                            "browser_handoff",
                            {"url": redirect_target, "reason": "redirected_html", "candidate_count": len(candidates)},
                        )
                # An intermediate HTML page must never be silently admitted as
                # the downloaded file after a shortlink hop. Give the bounded
                # static extractor one last chance, then require the browser
                # for timers, scripts, login, or CAPTCHA.
                if traversed_shortlink and resolved and all(looks_like_html_item(item) for item in resolved):
                    try:
                        candidates = fetch_and_extract(current)
                    except Exception:
                        candidates = []
                    candidates = [candidate for candidate in candidates if candidate not in visited]
                    if len(candidates) == 1:
                        current = candidates[0]
                        visited.add(current)
                        traversed_shortlink = True
                        continue
                    raise NeedsUser(
                        "Shortlink chain ended on an HTML/interstitial page that needs browser interaction",
                        "browser_handoff",
                        {"url": current, "reason": "html_interstitial", "candidate_count": len(candidates)},
                    )
                return resolved
            traversed_shortlink = True
            target = extracted[0].direct_url
            if not target or target in visited:
                raise EngineError("shortlink resolution loop detected")
            visited.add(target)
            current = target
        raise EngineError("shortlink chain exceeded the maximum hop count")

    def authenticate(self, provider_id: str, params: dict[str, Any]) -> Any:
        if provider_id not in self.manifests:
            raise KeyError("plugin not found")
        if not self.has_operation(provider_id, "authenticate"):
            raise EngineError("plugin does not provide account authentication")
        return self._call(provider_id, "authenticate", params)

    def media_metadata(self, url: str) -> Any:
        for provider_id in self.order:
            manifest = self.manifests[provider_id]
            if "media" not in manifest.get("roles", []) or not self.health[provider_id].get("enabled", True):
                continue
            if self._call(provider_id, "match", {"url": url}):
                if not self.has_operation(provider_id, "metadata"):
                    continue
                return self._call(provider_id, "metadata", {"url": url})
        raise EngineError("no media manifest plugin recognizes this URL")

    def close(self) -> None:
        for pool in list(self._pools.values()):
            pool.close()
        self._pools.clear()
        for process in list(self._processes.values()):
            process.close()
        self._processes.clear()
