from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "browser_extension" / "src"
SOURCE_FILES = ("protocol.ts", "browserApi.ts", "background.ts", "content.ts", "handoff.ts", "popup.ts")
PROTOCOL_VERSION = "browser-capture/1"
HOST_NAME = "ai.transfer.manager.browser"
BROWSERS = ("chrome", "edge", "firefox")
DEFAULT_CONFIG = {
    "schema": 1,
    "protocol_version": PROTOCOL_VERSION,
    "native_host_name": HOST_NAME,
    "extension_ids": {
        "chrome": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "edge": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "firefox": "transfer-manager@openai.example",
    },
    "allowed_origins": ["http://*/*", "https://*/*"],
    "passive_capture_default": False,
}


def load_config() -> dict[str, Any]:
    path = ROOT / "browser_extension" / "build.config.json"
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("browser extension build config must be an object")
    merged = dict(DEFAULT_CONFIG)
    merged.update(value)
    return merged


def _safe_output(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == ROOT or len(resolved.parts) <= 2:
        raise ValueError("browser extension output must not be the project root or a filesystem root")
    return resolved


def _source_text(name: str) -> str:
    path = SOURCE_ROOT / name
    if not path.is_file():
        raise FileNotFoundError(f"missing browser extension source: {path}")
    return path.read_text(encoding="utf-8")


def validate_common_sources() -> dict[str, Any]:
    sources = {name: _source_text(name) for name in SOURCE_FILES}
    required = {
        "protocol.ts": (PROTOCOL_VERSION, "ReplayQueue", "redactUrl", "isEligibleRequest"),
        "browserApi.ts": ("createBrowserAdapter", "BROWSER_TARGETS"),
        "background.ts": ("CaptureController", "reconnecting", "candidate_batch"),
        "content.ts": ("Capture media", "capture_candidate"),
        "popup.ts": ("createBrowserAdapter", "initPopup"),
    }
    missing = {name: [item for item in needles if item not in sources[name]] for name, needles in required.items()}
    missing = {name: values for name, values in missing.items() if values}
    if missing:
        raise ValueError(f"browser extension source contract is incomplete: {missing}")
    return {"protocol_version": PROTOCOL_VERSION, "sources": list(SOURCE_FILES), "common": True}


def _bundle_script(source_file: Path, out_file: Path, format_type: str = "iife") -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    npx = shutil.which("npx") or ("npx.cmd" if os.name == "nt" else "npx")
    cmd = [
        npx,
        "esbuild",
        str(source_file),
        f"--outfile={out_file}",
        "--bundle",
        f"--format={format_type}",
        "--target=chrome100",
    ]
    try:
        proc = subprocess.run(cmd, shell=(os.name == "nt"), capture_output=True, text=True, check=True)
        if out_file.is_file() and out_file.stat().st_size > 0:
            return
    except Exception:
        pass

    # Lightweight regex fallback if esbuild is unavailable
    text = source_file.read_text(encoding="utf-8")
    import re
    # Strip basic type annotations
    text = re.sub(r":\s*[A-Za-z0-9_<>\[\]|]+(\s*[,)=;])", r"\1", text)
    out_file.write_text(text, encoding="utf-8", newline="\n")


def _generate_icons(icon_dir: Path) -> None:
    icon_dir.mkdir(parents=True, exist_ok=True)
    icon_src = ROOT / "src-tauri" / "icons" / "icon.png"
    if not icon_src.is_file():
        return
    try:
        from PIL import Image
        with Image.open(icon_src) as img:
            for size in (16, 32, 48, 128):
                out_path = icon_dir / f"icon-{size}.png"
                img.resize((size, size), Image.Resampling.LANCZOS).save(out_path, format="PNG")
    except Exception:
        for size in (16, 32, 48, 128):
            out_path = icon_dir / f"icon-{size}.png"
            shutil.copyfile(icon_src, out_path)


def build_common_extension(output: str | Path) -> dict[str, Any]:
    validate_common_sources()
    destination = _safe_output(Path(output))
    common = destination / "common"
    common.mkdir(parents=True, exist_ok=True)

    # Bundle each script
    for name in SOURCE_FILES:
        target_js = common / name.replace(".ts", ".js")
        _bundle_script(SOURCE_ROOT / name, target_js, format_type="iife")

    return {"output": str(destination), **validate_common_sources()}


def _manifest(browser: str, config: dict[str, Any]) -> dict[str, Any]:
    extension_id = str(config["extension_ids"][browser])
    template_name = "manifest.firefox.json" if browser == "firefox" else "manifest.chrome.json"
    template_path = ROOT / "browser_extension" / template_name
    if template_path.exists():
        base = json.loads(template_path.read_text(encoding="utf-8"))
    else:
        base = {
            "manifest_version": 3,
            "name": "MossDL Capture",
            "version": str(config.get("extension_version", "0.1.0")),
            "description": "Opt-in browser capture for MossDL",
            "permissions": ["nativeMessaging", "storage", "tabs", "webRequest", "downloads"],
            "host_permissions": list(config["allowed_origins"]),
            "action": {
                "default_title": "MossDL Capture",
                "default_popup": "popup.html",
            },
        }
        if browser == "firefox":
            base["background"] = {"scripts": ["background.js"]}
        else:
            base["background"] = {"service_worker": "background.js"}

    base["version"] = str(config.get("extension_version", base.get("version", "0.1.0")))
    base["host_permissions"] = list(config["allowed_origins"])
    base["content_scripts"] = [
        {"matches": list(config["allowed_origins"]), "js": ["content.js"], "run_at": "document_idle", "all_frames": True},
        # Before the page's scripts: takes a captcha handoff ticket out of the URL.
        {"matches": list(config["allowed_origins"]), "js": ["handoff.js"], "run_at": "document_start", "all_frames": False},
    ]
    
    # Strip any unrecognized x-transfer-* keys from the web manifest
    for key in list(base.keys()):
        if key.startswith("x-transfer-"):
            base.pop(key, None)

    if browser == "firefox":
        base["browser_specific_settings"] = {
            "gecko": {
                "id": extension_id,
                "strict_min_version": "109.0",
                "data_collection_permissions": {"required": ["none"]},
            }
        }
        base["background"] = {"scripts": ["background.js"]}
    else:
        base.pop("browser_specific_settings", None)
        base["background"] = {"service_worker": "background.js"}

    return base


def _host_manifest(browser: str, config: dict[str, Any], host_path: Path) -> dict[str, Any]:
    extension_id = str(config["extension_ids"][browser])
    result: dict[str, Any] = {
        "name": str(config["native_host_name"]),
        "description": "MossDL browser capture native host",
        "path": str(host_path),
        "type": "stdio",
        "x-transfer-protocol-version": str(config["protocol_version"]),
    }
    if browser == "firefox":
        result["allowed_extensions"] = [extension_id]
    else:
        result["allowed_origins"] = [
            f"chrome-extension://{extension_id}/",
            f"chrome-extension://{extension_id}/*",
        ]
    return result


def build_extension(output: str | Path, *, host_path: str | Path | None = None, check: bool = False) -> dict[str, Any]:
    config = load_config()
    destination = _safe_output(Path(output))
    destination.mkdir(parents=True, exist_ok=True)
    
    # Build common bundle
    build_common_extension(destination)
    common = destination / "common"

    host = Path(host_path or (ROOT / "src-tauri" / "resources" / "transfer-engine.exe")).expanduser().resolve()
    if not host.is_absolute():
        raise ValueError("native host path must be absolute")

    outputs: dict[str, Any] = {}
    for browser in BROWSERS:
        browser_dir = destination / browser
        browser_dir.mkdir(exist_ok=True)

        # Copy JS files
        for name in SOURCE_FILES:
            js_name = name.replace(".ts", ".js")
            shutil.copyfile(common / js_name, browser_dir / js_name)

        # Copy HTML and CSS
        if (SOURCE_ROOT / "popup.html").is_file():
            shutil.copyfile(SOURCE_ROOT / "popup.html", browser_dir / "popup.html")
        if (SOURCE_ROOT / "popup.css").is_file():
            shutil.copyfile(SOURCE_ROOT / "popup.css", browser_dir / "popup.css")

        # Generate icons
        _generate_icons(browser_dir / "icons")

        # Write clean Manifest
        manifest_data = _manifest(browser, config)
        (browser_dir / "manifest.json").write_text(json.dumps(manifest_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        # Write extension internal config
        browser_config = {
            "schema": 1,
            "protocol_version": str(config["protocol_version"]),
            "browser": browser,
            "extension_id": str(config["extension_ids"][browser]),
            "passive_capture": bool(config["passive_capture_default"]),
        }
        (browser_dir / "config.json").write_text(json.dumps(browser_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        # Write native messaging host manifest
        (browser_dir / f"{config['native_host_name']}.json").write_text(
            json.dumps(_host_manifest(browser, config, host), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        outputs[browser] = {
            "extension": str(browser_dir),
            "manifest": str(browser_dir / "manifest.json"),
            "config": str(browser_dir / "config.json"),
            "native_host_manifest": str(browser_dir / f"{config['native_host_name']}.json"),
        }

    metadata = {
        "schema": 1,
        "protocol_version": config["protocol_version"],
        "native_host_name": config["native_host_name"],
        "host_path": str(host),
        "browsers": outputs,
        "passive_capture_default": bool(config["passive_capture_default"]),
    }
    (destination / "build.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if check:
        check_build(destination)
    return metadata


def check_build(output: str | Path) -> dict[str, Any]:
    destination = Path(output).resolve()
    metadata = json.loads((destination / "build.json").read_text(encoding="utf-8"))
    if metadata.get("protocol_version") != PROTOCOL_VERSION or set(metadata.get("browsers", {})) != set(BROWSERS):
        raise ValueError("browser extension build metadata is incomplete")
    for browser in BROWSERS:
        manifest = json.loads((destination / browser / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("manifest_version") != 3:
            raise ValueError(f"{browser} manifest must be manifest_version 3")
        config_path = destination / browser / "config.json"
        if not config_path.is_file():
            raise ValueError(f"{browser} config.json is missing")
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
        if config_data.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError(f"{browser} config has the wrong protocol version")
        host_name = str(metadata.get("native_host_name") or HOST_NAME)
        host_manifest = json.loads((destination / browser / f"{host_name}.json").read_text(encoding="utf-8"))
        if not Path(host_manifest["path"]).is_absolute():
            raise ValueError(f"{browser} native host path is not absolute")
    return metadata


def _registration_paths(registration_root: Path, browser: str, host_name: str) -> Path:
    return registration_root / browser / f"{host_name}.json"


def _register_windows_registry(browser: str, host_name: str, manifest_path: Path) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        reg_keys = {
            "chrome": r"Software\Google\Chrome\NativeMessagingHosts",
            "edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
            "firefox": r"Software\Mozilla\NativeMessagingHosts",
        }
        subkey = reg_keys.get(browser)
        if not subkey:
            return False
        full_key = f"{subkey}\\{host_name}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, full_key) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path.resolve()))
        return True
    except Exception:
        return False


def _unregister_windows_registry(browser: str, host_name: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import winreg
        reg_keys = {
            "chrome": r"Software\Google\Chrome\NativeMessagingHosts",
            "edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
            "firefox": r"Software\Mozilla\NativeMessagingHosts",
        }
        subkey = reg_keys.get(browser)
        if not subkey:
            return
        full_key = f"{subkey}\\{host_name}"
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, full_key)
    except Exception:
        pass


def install_native_host(output: str | Path, *, host_path: str | Path, registration_root: str | Path,
                        extension_id: str | None = None, register_system: bool = False) -> dict[str, Any]:
    metadata = build_extension(output, host_path=host_path, check=True)
    root = Path(registration_root).expanduser().resolve()
    host = Path(host_path).expanduser().resolve()
    if not host.is_absolute() or not host.exists() or not host.is_file():
        raise ValueError("native host executable must be an existing absolute file")
    installed: dict[str, str] = {}
    registered_system: dict[str, bool] = {}
    for browser in BROWSERS:
        target = _registration_paths(root, browser, str(metadata["native_host_name"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        source = Path(metadata["browsers"][browser]["native_host_manifest"])
        content = source.read_text(encoding="utf-8")
        if extension_id and browser in {"chrome", "edge"}:
            clean_ext_id = str(extension_id).strip()
            manifest_json = json.loads(content)
            allowed = [str(o).strip() for o in manifest_json.get("allowed_origins", []) if str(o).strip()]
            for o in (f"chrome-extension://{clean_ext_id}/", f"chrome-extension://{clean_ext_id}/*"):
                if o not in allowed:
                    allowed.append(o)
            manifest_json["allowed_origins"] = allowed
            content = json.dumps(manifest_json, indent=2, sort_keys=True) + "\n"
        elif extension_id and browser == "firefox":
            clean_ext_id = str(extension_id).strip()
            manifest_json = json.loads(content)
            allowed = [str(e).strip() for e in manifest_json.get("allowed_extensions", []) if str(e).strip()]
            if clean_ext_id not in allowed:
                allowed.append(clean_ext_id)
            manifest_json["allowed_extensions"] = allowed
            content = json.dumps(manifest_json, indent=2, sort_keys=True) + "\n"
        target.write_text(content, encoding="utf-8", newline="\n")
        installed[browser] = str(target)
        if register_system:
            registered_system[browser] = _register_windows_registry(browser, str(metadata["native_host_name"]), target)
    state = {
        "schema": 1,
        "native_host_name": metadata["native_host_name"],
        "protocol_version": PROTOCOL_VERSION,
        "registration_root": str(root),
        "files": installed,
        "system_registered": registered_system if register_system else False,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "registration.json").write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"installed": True, "idempotent": True, **state}


def registration_status(registration_root: str | Path) -> dict[str, Any]:
    root = Path(registration_root).expanduser().resolve()
    state_path = root / "registration.json"
    if not state_path.exists():
        return {"installed": False, "registration_root": str(root), "browsers": {browser: False for browser in BROWSERS}}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    browsers = {browser: bool(Path(state.get("files", {}).get(browser, "")).is_file()) for browser in BROWSERS}
    return {
        "installed": all(browsers.values()),
        "registration_root": str(root),
        "browsers": browsers,
        "native_host_name": state.get("native_host_name"),
        "protocol_version": state.get("protocol_version"),
    }


def uninstall_native_host(registration_root: str | Path) -> dict[str, Any]:
    root = Path(registration_root).expanduser().resolve()
    state_path = root / "registration.json"
    removed: list[str] = []
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for raw_path in state.get("files", {}).values():
            path = Path(raw_path).resolve()
            if path.is_file() and root in path.parents:
                path.unlink()
                removed.append(str(path))
        state_path.unlink()
        removed.append(str(state_path))
    for browser in BROWSERS:
        browser_dir = root / browser
        if browser_dir.is_dir() and not any(browser_dir.iterdir()):
            browser_dir.rmdir()
    return {
        "installed": False,
        "removed": removed,
        "registration_root": str(root),
        "browsers": {browser: False for browser in BROWSERS},
    }


def package_extension(output: str | Path) -> dict[str, Any]:
    """Build, then zip each browser's extension for its store.

    The native-messaging host manifest stays out: it is installed with the app
    and names a path on the user's machine, not part of the store listing.
    """
    import zipfile
    config = load_config()
    build_extension(output)
    packages: dict[str, str] = {}
    for browser in BROWSERS:
        folder = Path(output) / browser
        archive = Path(output) / f"{browser}-extension.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(folder.rglob("*")):
                if path.is_file() and path.name != f"{config['native_host_name']}.json":
                    zf.write(path, path.relative_to(folder).as_posix())
        packages[browser] = str(archive)
    return {"packages": packages}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and register the MossDL browser capture extension")
    parser.add_argument("command", nargs="?", choices=("build", "package", "install", "status", "uninstall"), default="build")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", default=str(ROOT / ".test-artifacts" / "browser-extension"))
    parser.add_argument("--host-path")
    parser.add_argument("--registration-root", default=str(Path.home() / ".transfer-manager" / "browser-registration"))
    parser.add_argument("--extension-id", help="Active Chrome unpacked extension ID to allow in host manifest")
    parser.add_argument("--register-system", action="store_true", help="Register native host in Windows Registry / system directories")
    args = parser.parse_args(argv)
    if args.command == "build":
        result = build_extension(args.output, host_path=args.host_path, check=args.check)
    elif args.command == "package":
        result = package_extension(args.output)
    elif args.command == "install":
        if not args.host_path:
            parser.error("install requires --host-path")
        result = install_native_host(
            args.output,
            host_path=args.host_path,
            registration_root=args.registration_root,
            extension_id=args.extension_id,
            register_system=args.register_system,
        )
    elif args.command == "status":
        result = registration_status(args.registration_root)
    else:
        result = uninstall_native_host(args.registration_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
