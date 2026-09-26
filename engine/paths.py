from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


def validate_directory(value: str | os.PathLike[str], base: str | Path,
                       create: bool = False) -> dict[str, Any]:
    """Validate a user-selected destination without rebasing absolute paths.

    Relative values are application-local conveniences. Drive, UNC, POSIX,
    removable, and mounted paths remain absolute and are never moved below the
    application directory.
    """
    raw = os.fspath(value).strip()
    if not raw:
        return {"valid": False, "error": "directory is empty"}
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(base).expanduser() / candidate
    candidate = candidate.resolve(strict=False)
    try:
        if candidate.exists() and not candidate.is_dir():
            return {"valid": False, "path": str(candidate), "error": "path is not a directory"}
        if not candidate.exists():
            if not create:
                parent = candidate.parent
                if not parent.exists():
                    return {"valid": False, "path": str(candidate), "error": "directory and parent do not exist"}
            else:
                candidate.mkdir(parents=True, exist_ok=True)
        if not os.access(candidate, os.W_OK):
            return {"valid": False, "path": str(candidate), "error": "directory is not writable"}
        free = shutil.disk_usage(candidate).free
        return {"valid": True, "path": str(candidate), "free_bytes": free}
    except (OSError, ValueError) as exc:
        return {"valid": False, "path": str(candidate), "error": str(exc)}


def effective_directory(value: str | None, base: str | Path, create: bool = True) -> str:
    result = validate_directory(value or "downloads", base, create=create)
    if not result.get("valid"):
        raise ValueError(result.get("error", "invalid directory"))
    return str(result["path"])
