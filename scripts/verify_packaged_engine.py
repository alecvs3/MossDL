"""Smoke the frozen engine from outside the source tree using a fresh data folder."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def verify_browser_transport(engine: Path) -> None:
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(engine))
    with tempfile.TemporaryDirectory(prefix="mossdl-browser-driver-") as directory:
        root = Path(directory)
        for name in archive.toc:
            relative = Path(name.replace("\\", "/"))
            if relative.parts[:2] != ("patchright", "driver"):
                continue
            if ".." in relative.parts or relative.is_absolute():
                raise ValueError("Unsafe browser transport archive path")
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extract(name))
        driver = root / "patchright/driver"
        subprocess.run([str(driver / "node.exe"), str(driver / "package/cli.js"), "--version"],
                       check=True, capture_output=True, timeout=15)
    print("Bundled browser transport starts successfully")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    resources = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "src-tauri/resources"
    engine = resources / "transfer-engine/transfer-engine.exe"
    verify_browser_transport(engine)
    environment = os.environ.copy()
    environment["TRANSFER_CORE_PATH"] = str(resources / "transfer-core/transfer-core.exe")
    environment["ARCHIVE_WORKER_PATH"] = str(resources / "archive-worker/archive-worker.exe")
    environment["TRANSFER_PLUGIN_DIRS"] = str(root / "plugins")
    with tempfile.TemporaryDirectory(prefix="mossdl-packaged-") as data:
        start = time.monotonic()
        response = subprocess.run(
            [str(engine), data], cwd=data, env=environment,
            input=json.dumps({"id": 1, "method": "ui_snapshot", "params": {}}) + "\n",
            capture_output=True, text=True, timeout=30,
        )
        if response.returncode or response.stderr:
            raise RuntimeError(f"Packaged engine failed: {response.returncode}\n{response.stderr}")
        snapshot = json.loads(response.stdout.splitlines()[0])
        if "error" in snapshot or snapshot.get("result", {}).get("tasks") != []:
            raise RuntimeError(f"Unexpected fresh-engine response: {snapshot}")
        print(f"Packaged engine smoke passed in {time.monotonic() - start:.2f}s")


if __name__ == "__main__":
    main()
