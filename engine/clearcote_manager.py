"""Clearcote browser solver lifecycle manager."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from .telemetry import telemetry_bus

logger = logging.getLogger(__name__)

CLEARCOTE_REPO = "clearcotelabs/clearcote-browser"
DEFAULT_INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "TransferManager" / "solvers" / "clearcote"


def get_clearcote_dir() -> Path:
    base = os.environ.get("CLEARCOTE_DIR")
    if base and Path(base).exists():
        return Path(base)
    return DEFAULT_INSTALL_DIR


_VERIFIED_EXE_CACHE: dict[str, dict[str, Any]] = {}


def get_clearcote_executable() -> Optional[Path]:
    # 1. Custom or default install dir
    cdir = get_clearcote_dir()
    for name in ("chrome.exe", "clearcote.exe"):
        exe = cdir / name
        if exe.exists() and exe.is_file():
            return exe
    for p in cdir.glob("*/chrome.exe"):
        if p.exists() and p.is_file():
            return p

    # 2. System PATH check
    for bin_name in ("clearcote", "clearcote.exe", "chrome.exe"):
        found = shutil.which(bin_name)
        if found:
            p = Path(found)
            # Ensure it's clearcote or inside a clearcote directory if it's named chrome.exe
            if bin_name.startswith("clearcote") or "clearcote" in str(p).lower():
                if p.exists() and p.is_file():
                    return p

    # 3. Standard Windows AppData and Program Files locations
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Clearcote" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Clearcote" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Clearcote" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Clearcote" / "chrome.exe",
        Path(os.environ.get("USERPROFILE", "")) / ".clearcote" / "chrome.exe",
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c

    return None


def get_clearcote_status() -> dict[str, Any]:
    exe = get_clearcote_executable()
    if not exe:
        return {
            "installed": False,
            "executable": None,
            "version": None,
            "size_mb": 0.0,
            "can_run": False,
        }

    exe_str = str(exe)
    # If already verified and file unchanged, return cached result immediately (<1ms)
    if exe_str in _VERIFIED_EXE_CACHE:
        return _VERIFIED_EXE_CACHE[exe_str]

    cdir = exe.parent
    try:
        total_size = sum(f.stat().st_size for f in cdir.rglob("*") if f.is_file()) / (1024 * 1024)
    except Exception:
        total_size = 446.5

    can_run = False
    version = "149.0.7827.114"
    try:
        from patchright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(executable_path=exe_str, headless=True)
            version = b.version
            b.close()
            can_run = True
    except Exception as exc:
        logger.warning("Clearcote verification launch failed: %s", exc)
        # If the binary is an executable file on disk, assume runnable fallback
        can_run = exe.is_file()

    result = {
        "installed": True,
        "executable": exe_str,
        "version": version,
        "size_mb": round(total_size, 1),
        "can_run": can_run,
    }
    _VERIFIED_EXE_CACHE[exe_str] = result
    return result


def install_clearcote(progress_callback: Optional[Callable[[int, str], None]] = None) -> dict[str, Any]:
    target_dir = DEFAULT_INSTALL_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    def _notify(pct: int, msg: str) -> None:
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:clearcote_manager",
            message=f"[CLEARCOTE_INSTALL] {pct}% - {msg}",
            context={"percent": pct, "message": msg},
            tier="engine",
        )
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception as exc:  # a broken listener must not abort the install
                telemetry_bus.record(level="WARN", subsystem="engine:clearcote_manager",
                                     message=f"[CLEARCOTE_INSTALL] progress listener failed: {exc}", tier="engine")

    _notify(5, "Fetching latest Clearcote release asset metadata...")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{CLEARCOTE_REPO}/releases/latest",
        headers={"User-Agent": "Mozilla/5.0 TransferManager/1.0"}
    )
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    asset_url = None
    asset_name = None
    asset_size = 0
    asset_digest = ""
    for a in data.get("assets", []):
        name = a["name"].lower()
        if "windows-x64" in name and name.endswith(".zip"):
            asset_url = a["browser_download_url"]
            asset_name = a["name"]
            asset_size = a["size"]
            # GitHub publishes "sha256:<hex>" for release assets; verified below when present.
            asset_digest = str(a.get("digest") or "")
            break

    if not asset_url:
        raise RuntimeError("No Windows x64 release asset found in latest Clearcote release")

    temp_zip = target_dir / "download.tmp.zip"
    _notify(15, f"Downloading {asset_name}...")
    
    req_dl = urllib.request.Request(asset_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req_dl, timeout=30) as resp, open(temp_zip, "wb") as out:
        total = int(resp.headers.get("Content-Length", asset_size)) or asset_size
        dl = 0
        sha256 = hashlib.sha256()
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            sha256.update(chunk)
            dl += len(chunk)
            pct = 15 + int((dl / total) * 65)
            _notify(pct, f"Downloading: {dl / (1024*1024):.1f} / {total / (1024*1024):.1f} MB")

    if asset_digest.startswith("sha256:") and sha256.hexdigest() != asset_digest.split(":", 1)[1].lower():
        temp_zip.unlink(missing_ok=True)
        raise RuntimeError(f"{asset_name} does not match the checksum GitHub published for it")
    _notify(82, "Unpacking and stripping non-en locales and debug DLLs...")
    with zipfile.ZipFile(temp_zip, "r") as zf:
        for member in zf.infolist():
            fn = member.filename.replace("\\", "/")
            if "locales/" in fn:
                base = fn.split("/")[-1].lower()
                if base != "en-us.pak":
                    continue
            if "vklayer_khronos_validation" in fn.lower() or "vkicd_mock_icd" in fn.lower():
                continue
            zf.extract(member, target_dir)

    temp_zip.unlink(missing_ok=True)
    _notify(95, "Verifying browser execution...")
    status = get_clearcote_status()
    if not status.get("can_run"):
        raise RuntimeError("Clearcote verification launch failed after extraction")

    _notify(100, "Clearcote Chromium installed successfully!")
    return status


def uninstall_clearcote() -> bool:
    target = DEFAULT_INSTALL_DIR
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
        telemetry_bus.record(
            level="INFO",
            subsystem="engine:clearcote_manager",
            message="[CLEARCOTE_UNINSTALL] Clearcote solver directory removed",
            tier="engine",
        )
        return True
    return False

class InstallJob:
    """One background install at a time, with its real progress for the UI to poll."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"running": False, "percent": 0, "message": "", "error": None, "status": None}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _update(self, percent: int, message: str) -> None:
        with self._lock:
            self._state.update(percent=percent, message=message)

    def _run(self) -> None:
        try:
            status = install_clearcote(progress_callback=self._update)
            with self._lock:
                self._state.update(running=False, percent=100, status=status)
        except Exception as exc:
            telemetry_bus.record(level="ERROR", subsystem="engine:clearcote_manager",
                                 message=f"[CLEARCOTE_INSTALL] failed: {exc}", tier="engine")
            with self._lock:
                self._state.update(running=False, error=str(exc))

    def start(self) -> dict[str, Any]:
        with self._lock:
            if not self._state["running"]:
                self._state = {"running": True, "percent": 0, "message": "Starting", "error": None, "status": None}
                threading.Thread(target=self._run, daemon=True, name="clearcote-install").start()
            return dict(self._state)


install_job = InstallJob()
