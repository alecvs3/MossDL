"""Read-only capture of the test environment, stored once per run.

Nothing here changes system state. Values that cannot be read are recorded
as None with the reason, so a missing field is visible rather than guessed.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import psutil


def _powershell(command: str) -> str | None:
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=20)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {type(exc).__name__}"


def _volume_of(path: Path) -> dict:
    target = str(path.resolve())
    best = None
    for part in psutil.disk_partitions(all=False):
        if target.lower().startswith(part.mountpoint.lower()) and (best is None or len(part.mountpoint) > len(best.mountpoint)):
            best = part
    if best is None:
        return {"path": target, "mount": None}
    usage = psutil.disk_usage(best.mountpoint)
    return {"path": target, "mount": best.mountpoint, "fstype": best.fstype, "device": best.device,
            "free_bytes": usage.free, "total_bytes": usage.total}


def capture(dest_root: Path) -> dict:
    windows = sys.platform == "win32"
    nics = {name: {"up": stats.isup, "speed_mbps": stats.speed, "mtu": stats.mtu}
            for name, stats in psutil.net_if_stats().items() if stats.isup}
    info = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "os": platform.platform(),
        "os_release": platform.release(),
        "python": sys.version.split()[0],
        "psutil": psutil.__version__,
        "cpu": _powershell("(Get-CimInstance Win32_Processor | Select-Object -First 1).Name") if windows else platform.processor(),
        "logical_cpus": psutil.cpu_count(),
        "physical_cpus": psutil.cpu_count(logical=False),
        "ram_bytes": psutil.virtual_memory().total,
        "ram_available_bytes": psutil.virtual_memory().available,
        "destination_volume": _volume_of(dest_root),
        "disk_model": _powershell("Get-PhysicalDisk | Select-Object FriendlyName,MediaType,BusType | ConvertTo-Json -Compress") if windows else None,
        "power_scheme": _powershell("powercfg /getactivescheme") if windows else None,
        "defender_realtime": _powershell("try { (Get-MpComputerStatus).RealTimeProtectionEnabled } catch { 'unreadable' }") if windows else None,
        "defender_exclusions_note": "record manually whether the destination folder is excluded (the harness never changes it)",
        "network_interfaces_up": nics,
        "boot_time": psutil.boot_time(),
        "load_at_start_pct": psutil.cpu_percent(interval=1.0),
        "hostname_hash": hashlib.sha256(platform.node().encode()).hexdigest()[:12],
        "env_overrides": {key: os.environ[key] for key in ("TRANSFER_CORE_PATH", "MOSSDL_REPO") if key in os.environ},
    }
    return info


if __name__ == "__main__":
    print(json.dumps(capture(Path.cwd()), indent=2))
