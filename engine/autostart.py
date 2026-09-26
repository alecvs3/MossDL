from __future__ import annotations

import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "TransferManagerClipboardWatcher"
APP_VALUE_NAME = "TransferManager"


def set_user_autostart(enabled: bool, *, command: str | None = None, data_dir: str | Path | None = None,
                       value_name: str = VALUE_NAME) -> dict[str, object]:
    """Register only the current user startup value, never a machine-wide entry."""
    if os.name != "nt":
        return {"enabled": False, "supported": False, "reason": "windows_only"}
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        if enabled:
            if not command:
                root = str(Path(data_dir or Path.cwd()).resolve())
                command = f'"{sys.executable}" -m engine.service "{root}"'
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, value_name)
            except FileNotFoundError:
                pass
        try:
            current = winreg.QueryValueEx(key, value_name)[0]
        except FileNotFoundError:
            current = None
    return {"enabled": bool(current), "supported": True, "command": current}


def get_user_autostart(value_name: str = VALUE_NAME) -> dict[str, object]:
    if os.name != "nt":
        return {"enabled": False, "supported": False, "command": None}
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            command = winreg.QueryValueEx(key, value_name)[0]
    except FileNotFoundError:
        command = None
    return {"enabled": bool(command), "supported": True, "command": command}
