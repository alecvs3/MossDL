"""Empirical Analysis Configuration and Log Location Registry.

Central single source of truth for all diagnostic log paths, runtime artifacts,
and test targets used by the Transfer Manager empirical analysis suite.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple


class LogPaths(NamedTuple):
    engine_jsonl: Path
    download_logs_dir: Path
    task_store_db: Path
    host_concurrency_audit: Path
    host_concurrency_profiles: Path


def get_workspace_root() -> Path:
    """Return the transfer workspace root directory."""
    return Path(__file__).resolve().parents[1]


def get_app_data_dir() -> Path:
    """Return the platform-specific application data directory for ai.transfer.manager."""
    app_data = os.environ.get("APPDATA")
    if app_data:
        return Path(app_data) / "ai.transfer.manager"
    return Path.home() / ".config" / "ai.transfer.manager"


def get_log_paths() -> LogPaths:
    """Return verified paths to all engine telemetry and download session logs."""
    workspace = get_workspace_root()
    app_data = get_app_data_dir()

    return LogPaths(
        engine_jsonl=app_data / "logs" / "engine.jsonl",
        download_logs_dir=workspace / "download_logs",
        task_store_db=app_data / "downloads.sqlite3",
        host_concurrency_audit=workspace / "logs" / "host_concurrency_audit.jsonl",
        host_concurrency_profiles=workspace / "logs" / "host_concurrency_profiles.json",
    )


# Download directory for multi-part lifecycle runs; MOSSDL_ANALYSIS_DEST overrides it.
DEFAULT_DESTINATION = Path(os.environ.get("MOSSDL_ANALYSIS_DEST") or Path.home() / "Downloads" / "MossDL analysis")
