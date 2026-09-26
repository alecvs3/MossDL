"""Content-bound checkpoint identity without persisting URLs or credentials."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def content_identity(item, validator: str | None) -> str:
    payload = [item.provider, item.source_url, item.size, validator, item.checksum, item.postprocess]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def file_identity(path: Path) -> list[int] | None:
    try:
        stat = path.stat()
        return [stat.st_dev, stat.st_ino, stat.st_size]
    except FileNotFoundError:
        return None


def sync_data(path: Path) -> None:
    # Writers use unbuffered file handles: all acknowledged progress is already
    # in the kernel before this durability barrier and checkpoint publication.
    with path.open('r+b', buffering=0) as handle:
        os.fsync(handle.fileno())
