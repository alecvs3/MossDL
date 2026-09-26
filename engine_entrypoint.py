import os
import sys
from pathlib import Path

from engine.telemetry import install_telemetry_hooks, telemetry_bus
from engine.service import serve


if __name__ == "__main__":
    install_telemetry_hooks()
    is_native = len(sys.argv) > 1 and (
        sys.argv[1] == "--native"
        or sys.argv[1].startswith(("chrome-extension://", "edge-extension://"))
        or sys.argv[1].endswith(".json")
    )
    if is_native:
        target_data_dir = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--native" else str(Path.home() / ".transfer-manager")
    elif len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        target_data_dir = sys.argv[1]
    else:
        target_data_dir = str(Path.cwd() / ".transfer-manager")

    telemetry_bus.set_log_file(Path(target_data_dir) / "logs" / "engine.jsonl")
    telemetry_bus.record(
        level="INFO",
        subsystem="engine:boot",
        message="Transfer Manager Python engine launching",
        context={
            "pid": os.getpid(),
            "python_version": sys.version,
            "platform": sys.platform,
            "argv": sys.argv,
            "cwd": str(Path.cwd()),
            "data_dir": target_data_dir,
        },
    )

    if len(sys.argv) > 1 and (
        sys.argv[1] in {"browser-install", "browser-status", "browser-uninstall", "rpc",
                        "serve-http", "serve-native", "plugins", "resolve", "add", "import",
                        "download", "plugin"}
        or sys.argv[1].startswith("--") and sys.argv[1] != "--native"
    ):
        from engine.cli import main
        raise SystemExit(main(sys.argv[1:]))

    if is_native:
        from engine.browser_bridge import serve_native
        serve_native(target_data_dir)
    else:
        serve(target_data_dir)


