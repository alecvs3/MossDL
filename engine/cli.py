from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from .service import (EngineService, authenticate_headless_token, pre_dispatch)


def resolve_default_data_dir() -> str:
    if os.environ.get("TRANSFER_DATA_DIR"):
        return os.environ["TRANSFER_DATA_DIR"]
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        app_data = Path(os.environ["APPDATA"]) / "ai.transfer.manager"
        if app_data.exists():
            return str(app_data)
    elif sys.platform == "darwin":
        app_data = Path.home() / "Library" / "Application Support" / "ai.transfer.manager"
        if app_data.exists():
            return str(app_data)
    else:
        app_data = Path.home() / ".config" / "ai.transfer.manager"
        if app_data.exists():
            return str(app_data)
    return str(Path.cwd() / ".transfer-manager")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transfer Manager engine")
    parser.add_argument("--data-dir", default=resolve_default_data_dir())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plugins")
    sub.add_parser("serve-http")
    serve_native_cmd = sub.add_parser("serve-native")
    serve_native_cmd.add_argument("--data-dir", default=argparse.SUPPRESS, help="override data directory for native session")
    serve_native_cmd.add_argument("--parent-window", help="parent window handle passed by browser")
    serve_native_cmd.add_argument("browser_args", nargs="*", help="browser origin or extra arguments")
    rpc = sub.add_parser("rpc", help="dispatch one authenticated JSON-RPC operation")
    rpc.add_argument("method")
    rpc.add_argument("--params", default="{}", help="JSON object of operation parameters")
    rpc.add_argument("--params-file", help="read the JSON parameter object from a file")
    rpc.add_argument("--token", help="API-client token; defaults to TRANSFER_MANAGER_API_TOKEN")
    rpc.add_argument("--client-id")
    rpc.add_argument("--idempotency-key")
    browser_install = sub.add_parser("browser-install", help="install the browser native-messaging host")
    browser_install.add_argument("--output")
    browser_install.add_argument("--host-path", required=True)
    browser_install.add_argument("--registration-root")
    browser_status = sub.add_parser("browser-status", help="report browser native-messaging registration")
    browser_status.add_argument("--registration-root")
    browser_uninstall = sub.add_parser("browser-uninstall", help="remove only browser native-messaging registration")
    browser_uninstall.add_argument("--registration-root")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("url")
    add = sub.add_parser("add")
    add.add_argument("url")
    add.add_argument("destination")
    container = sub.add_parser("import", help="import a text, batch, DLC, or crawljob container")
    container.add_argument("input", help="path to the container or - for stdin")
    container.add_argument("--format", choices=("text", "batch", "dlc", "crawljob"))
    container.add_argument("--container-id")
    container.add_argument("--provider-id", default="generic")
    container.add_argument("--name")
    download = sub.add_parser("download")
    download.add_argument("url")
    download.add_argument("destination")
    download.add_argument("--backend", choices=("custom", "rust"), default="rust")
    plugin = sub.add_parser("plugin")
    plugin_sub = plugin.add_subparsers(dest="plugin_command", required=True)
    scaffold = plugin_sub.add_parser("scaffold")
    scaffold.add_argument("provider_id")
    scaffold.add_argument("--host", required=True)
    scaffold.add_argument("--implementation", choices=("recipe", "python"), default="recipe")
    scaffold.add_argument("--output-root", default=str(Path.cwd() / "plugins"))
    plugin_test = plugin_sub.add_parser("test")
    plugin_test.add_argument("provider_id", nargs="?")
    args, _ = parser.parse_known_args(argv)
    if args.command == "serve-http":
        from .http_api import serve_http
        serve_http(args.data_dir)
        return 0
    if args.command == "serve-native":
        from .browser_bridge import serve_native
        target_dir = getattr(args, "data_dir", None) or resolve_default_data_dir()
        serve_native(str(target_dir))
        return 0
    if args.command in {"browser-install", "browser-status", "browser-uninstall"}:
        from scripts.build_browser_extension import install_native_host, registration_status, uninstall_native_host
        registration_root = args.registration_root or str(Path(args.data_dir) / "browser-registration")
        if args.command == "browser-install":
            output = args.output or str(Path(args.data_dir) / "browser-extension")
            result = install_native_host(output, host_path=args.host_path, registration_root=registration_root)
        elif args.command == "browser-status":
            result = registration_status(registration_root)
        else:
            result = uninstall_native_host(registration_root)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "plugin":
        if args.plugin_command == "scaffold":
            from .scaffold import scaffold_plugin
            output = {"created": str(scaffold_plugin(args.provider_id, args.host, args.output_root, args.implementation))}
        else:
            from .plugin_contracts import run_contracts
            output = run_contracts(args.provider_id)
        print(json.dumps(output, indent=2))
        return 0
    service = EngineService(args.data_dir)
    try:
        if args.command == "rpc":
            raw_params = Path(args.params_file).read_text(encoding="utf-8") if args.params_file else args.params
            params = json.loads(raw_params)
            if not isinstance(params, dict):
                raise ValueError("RPC params must be a JSON object")
            import os
            token = args.token or os.environ.get("TRANSFER_MANAGER_API_TOKEN")
            auth_context = authenticate_headless_token(service, token, token)
            if auth_context is None:
                raise PermissionError("invalid API credentials")
            client_id = args.client_id or auth_context["client_id"]
            request = {"jsonrpc": "2.0", "id": "cli-1", "method": args.method,
                       "params": params, "client_id": client_id}
            if args.idempotency_key:
                request["idempotency_key"] = args.idempotency_key
            output = {"jsonrpc": "2.0", "id": request["id"],
                      "result": pre_dispatch(service, request, auth_context)}
        elif args.command == "plugins":
            output = service.plugins.manifests_with_health()
        elif args.command == "resolve":
            output = [item.to_dict() for item in service.plugins.resolve(args.url)]
        elif args.command == "add":
            output = service.dispatch("add_task", {"url": args.url, "destination": args.destination})
        elif args.command == "import":
            payload = __import__("sys").stdin.read() if args.input == "-" else Path(args.input)
            output = service.dispatch("container_import", {"content": payload, "format": args.format,
                                                             "container_id": args.container_id,
                                                             "provider_id": args.provider_id, "name": args.name})
        else:
            items = service.plugins.resolve(args.url)
            output = []
            for item in items:
                if args.backend == "rust":
                    path = service.rust_backend.download(item, args.destination)
                else:
                    path = asyncio.run(service.custom_backend.download(item, args.destination))
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                output.append({"path": str(path), "size": path.stat().st_size, "sha256": digest, "provider": item.provider})
    finally:
        service.close()
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
