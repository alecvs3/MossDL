"""Empirical Analysis Suite for Transfer Manager.

Provides log parsing, automated Turnstile solver auditing, and
end-to-end multi-part download lifecycle validation.
"""

from .config import LogPaths, get_app_data_dir, get_log_paths, get_workspace_root
from .lifecycle_runner import LifecycleResult, LifecycleRunner
from .log_analyzer import LogAuditReport, audit_logs, format_report_summary, read_engine_logs

__all__ = [
    "LogPaths",
    "get_app_data_dir",
    "get_log_paths",
    "get_workspace_root",
    "LifecycleResult",
    "LifecycleRunner",
    "LogAuditReport",
    "audit_logs",
    "format_report_summary",
    "read_engine_logs",
]
