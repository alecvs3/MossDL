from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .file_classifier import classify_file
from .models import ArchiveJob, CaptureBatch, CollectionPlan, DownloadQueue, DownloadTask, ProviderHealthSnapshot, ResolutionContext, ResolvedItem, TransferLease
from .limits import normalize_bandwidth_rate
from .lifecycle import MAX_STAGE_HISTORY


CURRENT_SCHEMA_VERSION = 8

ARCHIVE_ACTIVE_STATES: tuple[str, ...] = ("running", "verifying", "extracting", "cleanup_pending")


class RevisionConflict(RuntimeError):
    """Raised when a caller tries to write an obsolete resource revision."""


class TaskStore:
    """SQLite persistence for tasks, plans, leases, archive jobs and event outbox."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, source_url TEXT NOT NULL, destination TEXT NOT NULL,
          display_name TEXT, password_ref TEXT, provider TEXT, state TEXT NOT NULL,
          size INTEGER, completed_bytes INTEGER NOT NULL DEFAULT 0, error TEXT,
          retry_count INTEGER NOT NULL DEFAULT 0, resolved_json TEXT NOT NULL DEFAULT '[]', route_profile_id TEXT,
          alternates_json TEXT NOT NULL DEFAULT '[]', priority INTEGER NOT NULL DEFAULT 0,
          queue_id TEXT NOT NULL DEFAULT 'default', queue_order INTEGER NOT NULL DEFAULT 0,
          scheduled_at REAL, paused_reason TEXT, category TEXT, duplicate_strategy TEXT NOT NULL DEFAULT 'skip',
          source_fingerprint TEXT, finished_at REAL, user_action TEXT, user_challenge_json TEXT NOT NULL DEFAULT '{}',
          request_headers_json TEXT NOT NULL DEFAULT '{}', referrer TEXT, browser_context_json TEXT NOT NULL DEFAULT '{}',
          selection_json TEXT NOT NULL DEFAULT 'null',
          account_ref TEXT, revision INTEGER NOT NULL DEFAULT 0, lease_id TEXT,
          heartbeat_at REAL, recovery_reason TEXT, integrity_json TEXT NOT NULL DEFAULT '{}',
          folder_path TEXT, speed_bytes_per_second REAL NOT NULL DEFAULT 0,
          average_speed_bytes_per_second REAL NOT NULL DEFAULT 0, eta_seconds REAL,
          backend TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, started_at REAL,
          telemetry_updated_at REAL, integrity_state TEXT NOT NULL DEFAULT 'unverified',
          package_key TEXT, package_part_number INTEGER, package_part_count INTEGER,
          package_leader_id TEXT,
          created_at REAL NOT NULL DEFAULT (unixepoch()), updated_at REAL NOT NULL DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS task_transitions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
          from_state TEXT, to_state TEXT NOT NULL, event_type TEXT NOT NULL, reason TEXT,
          created_at REAL NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS queues (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          paused INTEGER NOT NULL DEFAULT 0, max_active INTEGER, start_hour INTEGER, end_hour INTEGER,
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_items (
          task_id TEXT NOT NULL, item_id TEXT NOT NULL, item_index INTEGER NOT NULL,
          source_url TEXT NOT NULL, display_name TEXT NOT NULL, relative_path TEXT NOT NULL,
          size INTEGER, completed_bytes INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'queued',
          checksum TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL,
          PRIMARY KEY(task_id, item_id), FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS task_plans (
          task_id TEXT PRIMARY KEY, provider TEXT, destination TEXT NOT NULL,
          expected_bytes INTEGER NOT NULL DEFAULT 0, policy_json TEXT NOT NULL,
          credentials_ref TEXT, plan_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
          FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS segments (
          task_id TEXT NOT NULL, item_index INTEGER NOT NULL, segment_index INTEGER NOT NULL,
          start_byte INTEGER NOT NULL, end_byte INTEGER, completed INTEGER NOT NULL DEFAULT 0,
          completed_bytes INTEGER NOT NULL DEFAULT 0, validator TEXT,
          state TEXT NOT NULL DEFAULT 'queued', updated_at REAL NOT NULL,
          PRIMARY KEY(task_id, item_index, segment_index), FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS transfer_leases (
          lease_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, host TEXT NOT NULL, provider TEXT NOT NULL,
          account_ref TEXT, segment_count INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL,
          released_at REAL, created_at REAL NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS retry_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, item_index INTEGER,
          category TEXT NOT NULL, status_code INTEGER, delay_seconds REAL NOT NULL DEFAULT 0,
          error TEXT, created_at REAL NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS archive_jobs (
          id TEXT PRIMARY KEY, task_id TEXT, input_path TEXT NOT NULL, output_directory TEXT NOT NULL,
          format TEXT, password_ref TEXT, policy_json TEXT NOT NULL, state TEXT NOT NULL,
          progress_bytes INTEGER NOT NULL DEFAULT 0, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
          package_key TEXT, operation TEXT NOT NULL DEFAULT 'join_verify', part_manifest_json TEXT NOT NULL DEFAULT '[]',
          expected_size INTEGER, observed_size INTEGER, expected_digest TEXT, observed_digest TEXT,
          attempt INTEGER NOT NULL DEFAULT 0, recovery_reason TEXT, revision INTEGER NOT NULL DEFAULT 0,
          staging_path TEXT
        );
        CREATE TABLE IF NOT EXISTS event_outbox (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, task_id TEXT,
          payload_json TEXT NOT NULL, dedupe_key TEXT UNIQUE, created_at REAL NOT NULL, published_at REAL
        );
        CREATE TABLE IF NOT EXISTS quota_usage (
          usage_day TEXT PRIMARY KEY, bytes_reserved INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provider_attempts (
          task_id TEXT NOT NULL, source_fingerprint TEXT NOT NULL, provider_id TEXT NOT NULL,
          route_profile_id TEXT NOT NULL, outcome TEXT NOT NULL, status_code INTEGER,
          retry_after REAL, started_at REAL NOT NULL, ended_at REAL, error TEXT,
          PRIMARY KEY(task_id, source_fingerprint, provider_id, route_profile_id),
          FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS route_profiles (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, endpoint TEXT, credential_ref TEXT,
          region TEXT, healthcheck_url TEXT, enabled INTEGER NOT NULL DEFAULT 1, config_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS route_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, profile_id TEXT NOT NULL,
          outcome TEXT NOT NULL, reason TEXT, started_at REAL NOT NULL, ended_at REAL,
          FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS file_hashes (
          task_id TEXT NOT NULL, item_id TEXT NOT NULL, algorithm TEXT NOT NULL, digest TEXT NOT NULL,
          path TEXT NOT NULL, created_at REAL NOT NULL,
          PRIMARY KEY(task_id, item_id, algorithm), FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS plugin_health (
          plugin_id TEXT PRIMARY KEY, version TEXT, enabled INTEGER NOT NULL DEFAULT 1,
          quarantined INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
          last_error TEXT, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provider_accounts (
          id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, label TEXT NOT NULL,
          credential_ref TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          state TEXT NOT NULL DEFAULT 'unknown', expires_at REAL, quota_bytes INTEGER,
          used_bytes INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at REAL NOT NULL,
          config_json TEXT NOT NULL DEFAULT '{}', account_type TEXT NOT NULL DEFAULT 'credential',
          last_checked_at REAL, quota_reset_at REAL, health_json TEXT NOT NULL DEFAULT '{}',
          refresh_state TEXT NOT NULL DEFAULT 'unknown', scope_json TEXT NOT NULL DEFAULT '[]',
          quarantine_until REAL, priority INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS link_grabber (
          id TEXT PRIMARY KEY, url TEXT NOT NULL, normalized_url TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'clipboard', source_context TEXT,
          state TEXT NOT NULL DEFAULT 'pending', provider_id TEXT,
          title TEXT, error TEXT, selected INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL, updated_at REAL NOT NULL,
          UNIQUE(normalized_url)
        );
        CREATE TABLE IF NOT EXISTS capture_batches (
          batch_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, protocol_version TEXT NOT NULL,
          extension_origin TEXT NOT NULL, page_origin TEXT NOT NULL, page_url TEXT,
          session_ref TEXT, candidates_json TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'pending',
          acknowledged INTEGER NOT NULL DEFAULT 0, imported_task_ids_json TEXT NOT NULL DEFAULT '[]',
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bandwidth_profiles (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'global',
          scope_key TEXT, rate_bytes_per_second INTEGER NOT NULL DEFAULT 0,
          windows_json TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
          updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_sinks (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, endpoint TEXT NOT NULL,
          credential_ref TEXT, enabled INTEGER NOT NULL DEFAULT 1,
          event_types_json TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS event_deliveries (
          event_id INTEGER NOT NULL, sink_id TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'pending', last_error TEXT, delivered_at REAL,
          PRIMARY KEY(event_id, sink_id), FOREIGN KEY(event_id) REFERENCES event_outbox(id) ON DELETE CASCADE,
          FOREIGN KEY(sink_id) REFERENCES notification_sinks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS shortlink_chains (
          task_id TEXT NOT NULL, hop INTEGER NOT NULL, url_hash TEXT NOT NULL, host TEXT NOT NULL,
          provider TEXT, state TEXT NOT NULL, error TEXT, updated_at REAL NOT NULL,
          PRIMARY KEY(task_id, hop), FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS diagnostic_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, level TEXT NOT NULL,
          stage TEXT NOT NULL, message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS download_history (
          task_id TEXT PRIMARY KEY, finished_at REAL NOT NULL, record_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_download_history_finished ON download_history(finished_at DESC);
        CREATE TABLE IF NOT EXISTS captcha_challenges (
          id TEXT PRIMARY KEY, task_id TEXT, provider_id TEXT NOT NULL,
          captcha_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          params_json TEXT NOT NULL DEFAULT '{}', solution_json TEXT, solver_id TEXT,
          error TEXT, timeout_seconds REAL NOT NULL DEFAULT 90.0,
          created_at REAL NOT NULL, expires_at REAL NOT NULL, resolved_at REAL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS resolution_cache (
          cache_key TEXT PRIMARY KEY, source_url TEXT NOT NULL, provider_id TEXT, item_id TEXT,
          metadata_json TEXT NOT NULL DEFAULT '{}', expires_at REAL, refresh_policy TEXT NOT NULL,
          updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS collection_plans (
          id TEXT PRIMARY KEY, source_url TEXT NOT NULL, provider_id TEXT, page INTEGER NOT NULL DEFAULT 0,
          cursor TEXT, has_more INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'active',
          plan_json TEXT NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS crawl_nodes (
          crawl_id TEXT NOT NULL, node_id TEXT NOT NULL, parent_id TEXT, depth INTEGER NOT NULL DEFAULT 0,
          source_url TEXT NOT NULL, canonical_url TEXT NOT NULL, page INTEGER NOT NULL DEFAULT 0,
          cursor TEXT, folder_path TEXT NOT NULL DEFAULT '', package_path TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'discovered', outcome TEXT, node_json TEXT NOT NULL DEFAULT '{}',
          revision INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
          PRIMARY KEY(crawl_id, node_id), FOREIGN KEY(crawl_id) REFERENCES collection_plans(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS crawl_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, crawl_id TEXT NOT NULL, revision INTEGER NOT NULL,
          outcome TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
          FOREIGN KEY(crawl_id) REFERENCES collection_plans(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS provider_health_snapshots (
          provider_id TEXT PRIMARY KEY, success_rate REAL NOT NULL DEFAULT 0, total_attempts INTEGER NOT NULL DEFAULT 0,
          successes INTEGER NOT NULL DEFAULT 0, failure_categories_json TEXT NOT NULL DEFAULT '{}',
          last_known_version TEXT, quarantined INTEGER NOT NULL DEFAULT 0, last_error TEXT, checked_at REAL
        );
        CREATE TABLE IF NOT EXISTS api_idempotency (
          client_key TEXT NOT NULL, operation TEXT NOT NULL, request_hash TEXT NOT NULL,
          response_json TEXT NOT NULL, created_at REAL NOT NULL,
          PRIMARY KEY(client_key, operation)
        );
        CREATE TABLE IF NOT EXISTS workflow_jobs (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, steps_json TEXT NOT NULL DEFAULT '[]',
          progress REAL NOT NULL DEFAULT 0, error TEXT, revision INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workers (
          id TEXT PRIMARY KEY, protocol_version TEXT NOT NULL, capabilities_json TEXT NOT NULL DEFAULT '[]',
          state TEXT NOT NULL DEFAULT 'active', last_seen_at REAL NOT NULL, lease_id TEXT,
          metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS api_clients (
          id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
          scopes_json TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
          created_at REAL NOT NULL, revoked_at REAL
        );
        CREATE TABLE IF NOT EXISTS host_concurrency_profiles (
          host TEXT PRIMARY KEY,
          verified_ceiling INTEGER NOT NULL DEFAULT 1,
          denied_at_ceiling INTEGER,
          cooldown_until REAL NOT NULL DEFAULT 0.0,
          last_probe_at REAL NOT NULL DEFAULT 0.0,
          rejection_reasons_json TEXT NOT NULL DEFAULT '[]',
          updated_at REAL NOT NULL
        );
        """)
        # Existing databases predate route selection. SQLite has no IF NOT
        # EXISTS form for columns, so migrate the one additive field safely.
        task_columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "route_profile_id" not in task_columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN route_profile_id TEXT")
        if "alternates_json" not in task_columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN alternates_json TEXT NOT NULL DEFAULT '[]'")
        for name, definition in {
            "priority": "INTEGER NOT NULL DEFAULT 0", "queue_id": "TEXT NOT NULL DEFAULT 'default'",
            "queue_order": "INTEGER NOT NULL DEFAULT 0", "scheduled_at": "REAL", "paused_reason": "TEXT",
            "category": "TEXT", "duplicate_strategy": "TEXT NOT NULL DEFAULT 'skip'",
            "source_fingerprint": "TEXT", "finished_at": "REAL", "user_action": "TEXT",
            "user_challenge_json": "TEXT NOT NULL DEFAULT '{}'",
            "request_headers_json": "TEXT NOT NULL DEFAULT '{}'", "referrer": "TEXT",
            "browser_context_json": "TEXT NOT NULL DEFAULT '{}'",
            "selection_json": "TEXT NOT NULL DEFAULT 'null'",
            "account_ref": "TEXT", "revision": "INTEGER NOT NULL DEFAULT 0", "lease_id": "TEXT",
            "heartbeat_at": "REAL", "recovery_reason": "TEXT", "integrity_json": "TEXT NOT NULL DEFAULT '{}'",
            "folder_path": "TEXT", "speed_bytes_per_second": "REAL NOT NULL DEFAULT 0",
            "average_speed_bytes_per_second": "REAL NOT NULL DEFAULT 0", "eta_seconds": "REAL",
            "backend": "TEXT", "attempt_count": "INTEGER NOT NULL DEFAULT 0", "started_at": "REAL",
            "telemetry_updated_at": "REAL", "integrity_state": "TEXT NOT NULL DEFAULT 'unverified'",
            "package_key": "TEXT", "package_part_number": "INTEGER", "package_part_count": "INTEGER",
            "package_leader_id": "TEXT",
            "stage": "TEXT", "stage_detail_json": "TEXT NOT NULL DEFAULT '{}'",
            "stage_entered_at": "REAL", "stage_history_json": "TEXT NOT NULL DEFAULT '[]'",
        }.items():
            if name not in task_columns:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        account_columns = {row[1] for row in self.db.execute("PRAGMA table_info(provider_accounts)")}
        archive_columns = {row[1] for row in self.db.execute("PRAGMA table_info(archive_jobs)")}
        for name, definition in {
            "package_key": "TEXT", "operation": "TEXT NOT NULL DEFAULT 'join_verify'",
            "part_manifest_json": "TEXT NOT NULL DEFAULT '[]'", "expected_size": "INTEGER",
            "observed_size": "INTEGER", "expected_digest": "TEXT", "observed_digest": "TEXT",
            "attempt": "INTEGER NOT NULL DEFAULT 0", "recovery_reason": "TEXT",
            "revision": "INTEGER NOT NULL DEFAULT 0", "staging_path": "TEXT",
        }.items():
            if name not in archive_columns:
                self.db.execute(f"ALTER TABLE archive_jobs ADD COLUMN {name} {definition}")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_jobs_package_key ON archive_jobs(package_key) WHERE package_key IS NOT NULL")
        self._migrate_archive_jobs_to_orphan_safe_history()
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_task_transitions_task_id ON task_transitions(task_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_diagnostic_logs_task_id ON diagnostic_logs(task_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_event_outbox_published ON event_outbox(published_at)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_event_outbox_task ON event_outbox(task_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_fingerprint_state ON tasks(source_fingerprint, state)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(queue_id, priority DESC, queue_order)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_package_key ON tasks(package_key)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_link_grabber_created ON link_grabber(created_at)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_link_grabber_state ON link_grabber(state)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_diagnostic_logs_lookup ON diagnostic_logs(task_id, created_at)")
        for name, definition in {
            "account_type": "TEXT NOT NULL DEFAULT 'credential'",
            "last_checked_at": "REAL",
            "quota_reset_at": "REAL",
            "health_json": "TEXT NOT NULL DEFAULT '{}'",
            "refresh_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "scope_json": "TEXT NOT NULL DEFAULT '[]'",
            "quarantine_until": "REAL",
            "priority": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in account_columns:
                self.db.execute(f"ALTER TABLE provider_accounts ADD COLUMN {name} {definition}")
        segment_columns = {row[1] for row in self.db.execute("PRAGMA table_info(segments)")}
        if "completed_bytes" not in segment_columns:
            self.db.execute("ALTER TABLE segments ADD COLUMN completed_bytes INTEGER NOT NULL DEFAULT 0")
        if "validator" not in segment_columns:
            self.db.execute("ALTER TABLE segments ADD COLUMN validator TEXT")
        self.db.execute("INSERT OR IGNORE INTO queues(id,name,created_at,updated_at) VALUES ('default','Default',?,?)", (time.time(), time.time()))
        current_schema = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if current_schema < CURRENT_SCHEMA_VERSION:
            self.db.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        self.db.commit()
        self._deleted_task_ids: set[str] = set()
        self.release_expired_leases()
        try:
            from .telemetry import telemetry_bus
            telemetry_bus.record(
                level="INFO",
                subsystem="engine:db",
                message="TaskStore database initialized and migrations applied",
                context={"path": self.path, "schema_version": CURRENT_SCHEMA_VERSION},
            )
        except Exception:
            pass

    def _migrate_archive_jobs_to_orphan_safe_history(self) -> None:
        """Keep archive audit rows when their originating task is deleted.

        Older schemas used ``ON DELETE CASCADE`` for archive jobs, which made
        the archive lifecycle disappear when the user removed a completed
        download row. Rebuild that one table without the task foreign key;
        ``task_id`` remains a useful historical reference but is intentionally
        allowed to outlive the task.
        """
        fk_rows = self.db.execute("PRAGMA foreign_key_list(archive_jobs)").fetchall()
        if not any(str(row[6]).upper() == "CASCADE" for row in fk_rows):
            return
        columns = (
            "id,task_id,input_path,output_directory,format,password_ref,policy_json,state,"
            "progress_bytes,error,created_at,updated_at,package_key,operation,part_manifest_json,"
            "expected_size,observed_size,expected_digest,observed_digest,attempt,recovery_reason,"
            "revision,staging_path"
        )
        self.db.execute("DROP INDEX IF EXISTS idx_archive_jobs_package_key")
        self.db.execute("ALTER TABLE archive_jobs RENAME TO archive_jobs_legacy")
        self.db.execute("""CREATE TABLE archive_jobs (
          id TEXT PRIMARY KEY, task_id TEXT, input_path TEXT NOT NULL, output_directory TEXT NOT NULL,
          format TEXT, password_ref TEXT, policy_json TEXT NOT NULL, state TEXT NOT NULL,
          progress_bytes INTEGER NOT NULL DEFAULT 0, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
          package_key TEXT, operation TEXT NOT NULL DEFAULT 'join_verify', part_manifest_json TEXT NOT NULL DEFAULT '[]',
          expected_size INTEGER, observed_size INTEGER, expected_digest TEXT, observed_digest TEXT,
          attempt INTEGER NOT NULL DEFAULT 0, recovery_reason TEXT, revision INTEGER NOT NULL DEFAULT 0,
          staging_path TEXT
        )""")
        self.db.execute(f"INSERT INTO archive_jobs ({columns}) SELECT {columns} FROM archive_jobs_legacy")
        self.db.execute("DROP TABLE archive_jobs_legacy")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_jobs_package_key ON archive_jobs(package_key) WHERE package_key IS NOT NULL")

    def close(self) -> None:
        with self._lock:
            self.db.close()
        try:
            from .telemetry import telemetry_bus
            telemetry_bus.record(
                level="INFO",
                subsystem="engine:db",
                message="TaskStore database closed",
                context={"path": self.path},
            )
        except Exception:
            pass


    def _save_task_unlocked(self, task: DownloadTask) -> None:
        if task.id in self._deleted_task_ids:
            return
        self.db.execute("""INSERT INTO tasks
          (id, source_url, destination, display_name, password_ref, provider, state, size,
           completed_bytes, error, retry_count, resolved_json, route_profile_id, alternates_json,
           priority, queue_id, queue_order, scheduled_at, paused_reason, category, duplicate_strategy,
           source_fingerprint, finished_at, user_action, user_challenge_json, request_headers_json, referrer,
           browser_context_json, selection_json, account_ref, revision, lease_id, heartbeat_at,
           recovery_reason, integrity_json, folder_path, speed_bytes_per_second,
           average_speed_bytes_per_second, eta_seconds, backend, attempt_count, started_at,
           telemetry_updated_at, integrity_state, package_key, package_part_number,
           package_part_count, package_leader_id, stage, stage_detail_json,
           stage_entered_at, stage_history_json, updated_at)
         VALUES (
           ?, ?, ?, ?,
           ?, ?, ?, ?,
           ?, ?, ?, ?,
           ?, ?, ?, ?,
           ?, ?, ?, ?,
           ?, ?, ?, ?,
           ?, ?, ?, ?,
           ?, ?, ?, ?,
           ?, ?, ?, ?,
           ?, ?, ?, ?,
           ?, ?, ?, ?,
           ?, ?, ?, ?,
           ?, ?, ?, ?, unixepoch())
        ON CONFLICT(id) DO UPDATE SET source_url=excluded.source_url, destination=excluded.destination,
          display_name=excluded.display_name, password_ref=excluded.password_ref, provider=excluded.provider,
          state=excluded.state, size=excluded.size, completed_bytes=excluded.completed_bytes, error=excluded.error,
          retry_count=excluded.retry_count, resolved_json=excluded.resolved_json, route_profile_id=excluded.route_profile_id,
          alternates_json=excluded.alternates_json, priority=excluded.priority, queue_id=excluded.queue_id,
          queue_order=excluded.queue_order, scheduled_at=excluded.scheduled_at, paused_reason=excluded.paused_reason,
          category=excluded.category, duplicate_strategy=excluded.duplicate_strategy,
          source_fingerprint=excluded.source_fingerprint, finished_at=excluded.finished_at,
          user_action=excluded.user_action, user_challenge_json=excluded.user_challenge_json,
          request_headers_json=excluded.request_headers_json, referrer=excluded.referrer,
           browser_context_json=excluded.browser_context_json,
           selection_json=excluded.selection_json,
           account_ref=excluded.account_ref, revision=excluded.revision, lease_id=excluded.lease_id,
           heartbeat_at=excluded.heartbeat_at, recovery_reason=excluded.recovery_reason,
           integrity_json=excluded.integrity_json, folder_path=excluded.folder_path,
           speed_bytes_per_second=excluded.speed_bytes_per_second,
           average_speed_bytes_per_second=excluded.average_speed_bytes_per_second,
           eta_seconds=excluded.eta_seconds, backend=excluded.backend,
           attempt_count=excluded.attempt_count, started_at=excluded.started_at,
           telemetry_updated_at=excluded.telemetry_updated_at, integrity_state=excluded.integrity_state,
            package_key=excluded.package_key, package_part_number=excluded.package_part_number,
            package_part_count=excluded.package_part_count, package_leader_id=excluded.package_leader_id,
            stage=excluded.stage, stage_detail_json=excluded.stage_detail_json,
            stage_entered_at=excluded.stage_entered_at, stage_history_json=excluded.stage_history_json,
            updated_at=unixepoch()""",
           (task.id, task.source_url, task.destination, task.display_name, task.password_ref, task.provider,
            task.state, task.size, task.completed_bytes, task.error, task.retry_count,
            json.dumps([self._persisted_item(item) for item in task.resolved]), task.route_profile_id,
            json.dumps(self._strip_ephemeral(task.alternate_urls)), task.priority, task.queue_id, task.queue_order, task.scheduled_at,
            task.paused_reason, task.category, task.duplicate_strategy, task.source_fingerprint, task.finished_at,
            task.user_action, json.dumps(self._strip_ephemeral(task.user_challenge)), json.dumps(self._strip_ephemeral(task.request_headers)), task.referrer,
            json.dumps(self._strip_ephemeral(task.browser_context)), json.dumps(task.selected_item_ids),
            task.account_ref, task.revision, task.lease_id, task.heartbeat_at, task.recovery_reason,
            json.dumps(self._strip_ephemeral(task.integrity)), task.folder_path, task.speed_bytes_per_second,
            task.average_speed_bytes_per_second, task.eta_seconds, task.backend, task.attempt_count,
            task.started_at, task.telemetry_updated_at, task.integrity_state, task.package_key,
            task.package_part_number, task.package_part_count, task.package_leader_id,
            task.stage, json.dumps(self._strip_ephemeral(task.stage_detail)),
            task.stage_entered_at, json.dumps((task.stage_history or [])[-MAX_STAGE_HISTORY:])) )
        for index, item in enumerate(task.resolved):
            item_id = item.item_id or f"{index}:{item.display_name}"
            self.db.execute("""INSERT INTO task_items
              (task_id,item_id,item_index,source_url,display_name,relative_path,size,checksum,metadata_json,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(task_id,item_id) DO UPDATE SET item_index=excluded.item_index,
              display_name=excluded.display_name,relative_path=excluded.relative_path,size=excluded.size,
              checksum=excluded.checksum,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
              (task.id, item_id, index, item.source_url, item.display_name, item.relative_path, item.size,
               item.checksum, json.dumps(self._strip_ephemeral(item.metadata)), time.time()))

    @staticmethod
    def _persisted_item(item: ResolvedItem) -> dict[str, Any]:
        value = item.to_dict()
        return TaskStore._strip_ephemeral(value)

    @staticmethod
    def _strip_ephemeral(value: Any, key: str = "") -> Any:
        """Remove signed/direct URLs and secret-bearing values recursively."""
        low = key.lower()
        if low in {"media_plan", "segments", "variants", "selected_variant"}:
            return TaskStore._strip_nested_urls(value)
        if low in {"direct_url", "signed_url", "final_url"}:
            return None
        if low in {"authorization", "proxy-authorization", "cookie", "cookies", "set-cookie", "x-api-key",
                   "token", "access_token", "refresh_token"}:
            return None
        if isinstance(value, dict):
            result = {}
            for name, item in value.items():
                if str(name).lower() in {"authorization", "proxy-authorization", "cookie", "cookies", "set-cookie", "x-api-key",
                                         "token", "access_token", "refresh_token"}:
                    continue
                result[str(name)] = TaskStore._strip_ephemeral(item, str(name))
            return result
        if isinstance(value, list):
            return [TaskStore._strip_ephemeral(item, key) for item in value]
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            parsed = urlsplit(value)
            query = [(name, item) for name, item in parse_qsl(parsed.query, keep_blank_values=True)
                     if name.lower() not in {"sig", "signature", "token", "expires", "expiry", "x-amz-signature", "x-amz-credential", "x-amz-security-token"}]
            if len(query) != len(parse_qsl(parsed.query, keep_blank_values=True)):
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        return value

    @staticmethod
    def _strip_nested_urls(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(name): (None if str(name).lower() in {"url", "direct_url", "signed_url", "final_url"}
                                else TaskStore._strip_nested_urls(item)) for name, item in value.items()}
        if isinstance(value, list):
            return [TaskStore._strip_nested_urls(item) for item in value]
        return value

    def save(self, task: DownloadTask) -> None:
        with self._lock:
            task.revision = max(1, int(task.revision) + 1)
            self._save_task_unlocked(task)
            self.db.commit()

    def save_progress(self, task: DownloadTask) -> None:
        """Persist stream progress/telemetry/stage without rewriting scheduler-owned columns.

        Progress ticks must never clobber a concurrent pause/cancel state (the
        download thread holds a stale ``task`` object), and must not re-upsert
        every resolved item on the 250 ms cadence (write amplification)."""
        if task.id in self._deleted_task_ids:
            return
        with self._lock:
            self.db.execute(
                """UPDATE tasks SET completed_bytes=?, size=?, speed_bytes_per_second=?,
                   average_speed_bytes_per_second=?, eta_seconds=?, telemetry_updated_at=?,
                   integrity_state=?, stage=?, stage_detail_json=?, stage_entered_at=?,
                   stage_history_json=?, user_challenge_json=?, updated_at=unixepoch()
                   WHERE id=?""",
                (task.completed_bytes, task.size, task.speed_bytes_per_second,
                 task.average_speed_bytes_per_second, task.eta_seconds,
                 task.telemetry_updated_at, task.integrity_state,
                 task.stage, json.dumps(self._strip_ephemeral(task.stage_detail or {})),
                 task.stage_entered_at,
                 json.dumps((task.stage_history or [])[-MAX_STAGE_HISTORY:]),
                 json.dumps(self._strip_ephemeral(task.user_challenge or {})),
                 task.id))
            self.db.commit()

    def save_if_revision(self, task: DownloadTask, expected_revision: int) -> None:
        """Compare-and-save a task without changing the legacy save contract."""
        if task.id in self._deleted_task_ids:
            return
        with self._lock:
            row = self.db.execute("SELECT revision FROM tasks WHERE id=?", (task.id,)).fetchone()
            actual = int(row["revision"]) if row else 0
            if actual != int(expected_revision):
                raise RevisionConflict(f"task revision conflict: expected {expected_revision}, got {actual}")
            task.revision = max(1, actual + 1)
            self._save_task_unlocked(task)
            self.db.commit()

    def save_with_event(self, task: DownloadTask, event_type: str, payload: dict[str, Any],
                        dedupe_key: str | None = None, from_state: str | None = None) -> None:
        if task.id in self._deleted_task_ids:
            return
        with self._lock:
            task.revision = max(1, int(task.revision) + 1)
            self._save_task_unlocked(task)
            self._record_transition_unlocked(task.id, task.revision, from_state, task.state, event_type, payload.get("reason"))
            event_payload = dict(payload)
            event_payload.setdefault("resource_id", task.id)
            event_payload["revision"] = task.revision
            self._enqueue_event_unlocked(event_type, task.id, event_payload, dedupe_key)
            self.db.commit()

    def save_with_event_if_revision(self, task: DownloadTask, expected_revision: int,
                                    event_type: str, payload: dict[str, Any],
                                    dedupe_key: str | None = None,
                                    from_state: str | None = None) -> None:
        """Compare-and-save plus transition/outbox write as one transaction."""
        if task.id in self._deleted_task_ids:
            return
        with self._lock:
            row = self.db.execute("SELECT revision FROM tasks WHERE id=?", (task.id,)).fetchone()
            actual = int(row["revision"]) if row else 0
            if actual != int(expected_revision):
                raise RevisionConflict(f"task revision conflict: expected {expected_revision}, got {actual}")
            task.revision = max(1, actual + 1)
            self._save_task_unlocked(task)
            self._record_transition_unlocked(task.id, task.revision, from_state, task.state, event_type, payload.get("reason"))
            event_payload = dict(payload)
            event_payload.setdefault("resource_id", task.id)
            event_payload["revision"] = task.revision
            self._enqueue_event_unlocked(event_type, task.id, event_payload, dedupe_key)
            self.db.commit()

    def _record_transition_unlocked(self, task_id: str, revision: int, from_state: str | None,
                                    to_state: str, event_type: str, reason: str | None = None) -> None:
        if task_id in self._deleted_task_ids:
            return
        self.db.execute(
            "INSERT INTO task_transitions(task_id,revision,from_state,to_state,event_type,reason,created_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, revision, from_state, to_state, event_type, reason, time.time()))

    def record_transition(self, task_id: str, revision: int, from_state: str | None,
                          to_state: str, event_type: str, reason: str | None = None) -> None:
        if task_id in self._deleted_task_ids:
            return
        with self._lock:
            self._record_transition_unlocked(task_id, revision, from_state, to_state, event_type, reason)
            self.db.commit()

    def list_transitions(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM task_transitions WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, max(1, min(int(limit), 2000))))]

    def heartbeat_task(self, task_id: str, lease_id: str | None = None) -> bool:
        if task_id in self._deleted_task_ids:
            return False
        with self._lock:
            cur = self.db.execute(
                "UPDATE tasks SET heartbeat_at=?,lease_id=COALESCE(?,lease_id),updated_at=unixepoch() WHERE id=?",
                (time.time(), lease_id, task_id))
            self.db.commit()
            return cur.rowcount == 1

    def recover_stale_tasks(self, stale_after: float = 90.0) -> list[str]:
        cutoff = time.time() - max(1.0, float(stale_after))
        with self._lock:
            rows = self.db.execute(
                "SELECT id FROM tasks WHERE state IN ('resolving','preflight','downloading','verifying','postprocessing','retrying') "
                "AND (heartbeat_at IS NULL OR heartbeat_at<?)", (cutoff,)).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                self.db.executemany(
                    "UPDATE tasks SET state='queued',error=?,recovery_reason=?,lease_id=NULL,heartbeat_at=NULL,"
                    "revision=revision+1,updated_at=unixepoch() WHERE id=?",
                    [("Recovered after stale worker lease", "stale_lease", task_id) for task_id in ids])
            self.db.commit()
            return ids

    def get_idempotent(self, client_key: str, operation: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT response_json FROM api_idempotency WHERE client_key=? AND operation=?",
                (client_key, operation)).fetchone()
            return json.loads(row["response_json"]) if row else None

    def get_idempotency_record(self, client_key: str, operation: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT request_hash,response_json,created_at FROM api_idempotency WHERE client_key=? AND operation=?",
                (client_key, operation)).fetchone()
            return dict(row) if row else None

    def save_idempotent(self, client_key: str, operation: str, request_hash: str, response: Any) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO api_idempotency(client_key,operation,request_hash,response_json,created_at) VALUES (?,?,?,?,?)",
                (client_key, operation, request_hash, json.dumps(response), time.time()))
            self.db.commit()

    def migration_status(self) -> dict[str, Any]:
        with self._lock:
            version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
            tables = [row["name"] for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {"version": version, "tables": tables}

    def save_workflow(self, job: Any) -> dict[str, Any]:
        value = job.to_dict() if hasattr(job, "to_dict") else dict(job)
        with self._lock:
            existing = self.db.execute("SELECT revision FROM workflow_jobs WHERE id=?", (value["id"],)).fetchone()
            requested_revision = int(value.get("revision", 0))
            previous_revision = int(existing["revision"] or 0) if existing else 0
            value["revision"] = max(requested_revision, previous_revision + (1 if not existing else 0))
            value["updated_at"] = time.time()
            self.db.execute("""INSERT INTO workflow_jobs
              (id,kind,state,steps_json,progress,error,revision,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,state=excluded.state,
              steps_json=excluded.steps_json,progress=excluded.progress,error=excluded.error,
              revision=excluded.revision,updated_at=excluded.updated_at""",
              (value["id"], value["kind"], value.get("state", "queued"), json.dumps(self._strip_ephemeral(value.get("steps", []))),
               float(value.get("progress", 0)), value.get("error"), value["revision"],
               float(value.get("created_at", time.time())), value["updated_at"]))
            self.db.commit()
        return self.get_workflow(value["id"]) or value

    def get_workflow(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM workflow_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["steps"] = json.loads(value.pop("steps_json") or "[]")
        return value

    def list_workflows(self, state: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sql, args = "SELECT * FROM workflow_jobs", []
            if state:
                sql += " WHERE state=?"; args.append(state)
            sql += " ORDER BY updated_at DESC"
            rows = list(self.db.execute(sql, args))
        result = []
        for row in rows:
            value = dict(row); value["steps"] = json.loads(value.pop("steps_json") or "[]"); result.append(value)
        return result

    def backup_database(self, destination: str | Path) -> str:
        target = Path(destination).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            backup = sqlite3.connect(str(target))
            try:
                self.db.backup(backup)
                backup.commit()
            finally:
                backup.close()
        return str(target)

    def restore_database(self, source: str | Path, destination: str | Path) -> str:
        """Validate a backup and restore it to a new path; never overwrite the active DB."""
        source_path = Path(source).resolve()
        destination_path = Path(destination).resolve()
        active_path = Path(self.path).resolve()
        if source_path == active_path or destination_path == active_path or source_path == destination_path:
            raise ValueError("restore source and destination must be separate from the active database")
        if not source_path.is_file():
            raise FileNotFoundError(str(source_path))
        check = sqlite3.connect(str(source_path))
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup failed SQLite integrity check")
            version = int(check.execute("PRAGMA user_version").fetchone()[0])
            if version > CURRENT_SCHEMA_VERSION:
                raise ValueError(f"backup schema version {version} is newer than this engine")
        finally:
            check.close()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        source_db = sqlite3.connect(str(source_path))
        target_db = sqlite3.connect(str(destination_path))
        try:
            source_db.backup(target_db)
            target_db.commit()
        finally:
            target_db.close()
            source_db.close()
        return str(destination_path)

    def save_worker(self, worker: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self.db.execute("""INSERT INTO workers(id,protocol_version,capabilities_json,state,last_seen_at,lease_id,metadata_json)
              VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET protocol_version=excluded.protocol_version,
              capabilities_json=excluded.capabilities_json,state=excluded.state,last_seen_at=excluded.last_seen_at,
              lease_id=excluded.lease_id,metadata_json=excluded.metadata_json""",
              (worker["id"], worker.get("protocol_version", "v1"), json.dumps(worker.get("capabilities", [])),
               worker.get("state", "active"), now, worker.get("lease_id"),
               json.dumps(self._strip_ephemeral(worker.get("metadata", {})))))
            self.db.commit()
        return self.get_worker(worker["id"]) or worker

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["capabilities"] = json.loads(value.pop("capabilities_json") or "[]")
        value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
        return value

    def list_workers(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = [row["id"] for row in self.db.execute("SELECT id FROM workers ORDER BY id")]
        return [self.get_worker(worker_id) for worker_id in ids]

    def lease_worker(self, worker_id: str, lease_id: str) -> dict[str, Any] | None:
        with self._lock:
            self.db.execute("UPDATE workers SET lease_id=?,state='leased',last_seen_at=? WHERE id=? AND state='active'",
                            (lease_id, time.time(), worker_id))
            self.db.commit()
        return self.get_worker(worker_id)

    def heartbeat_worker(self, worker_id: str, lease_id: str | None = None) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE workers SET last_seen_at=? WHERE id=? AND (lease_id IS NULL OR lease_id=?)",
                                  (time.time(), worker_id, lease_id))
            self.db.commit()
            return bool(cur.rowcount)

    def release_worker(self, worker_id: str, lease_id: str | None = None, state: str = "active") -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE workers SET lease_id=NULL,state=?,last_seen_at=? WHERE id=? AND (lease_id IS NULL OR lease_id=?)",
                                  (state, time.time(), worker_id, lease_id))
            self.db.commit()
            return bool(cur.rowcount)

    def save_api_client(self, client: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.db.execute("""INSERT INTO api_clients(id,token_hash,label,scopes_json,enabled,created_at,revoked_at)
              VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,
              scopes_json=excluded.scopes_json,enabled=excluded.enabled,revoked_at=excluded.revoked_at""",
              (client["id"], client["token_hash"], client.get("label", client["id"]),
               json.dumps(sorted(set(client.get("scopes", [])))), int(client.get("enabled", True)),
               client.get("created_at", time.time()), client.get("revoked_at")))
            self.db.commit()
        return self.get_api_client(client["id"]) or client

    def get_api_client(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM api_clients WHERE id=?", (client_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        value["scopes"] = json.loads(value.pop("scopes_json") or "[]")
        value.pop("token_hash", None)
        return value

    def find_api_client_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM api_clients WHERE token_hash=? AND enabled=1 AND revoked_at IS NULL",
                                  (token_hash,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["scopes"] = json.loads(value.pop("scopes_json") or "[]")
        value.pop("token_hash", None)
        value["enabled"] = bool(value["enabled"])
        return value

    def list_api_clients(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = [row["id"] for row in self.db.execute("SELECT id FROM api_clients ORDER BY created_at")]
        return [self.get_api_client(client_id) for client_id in ids]

    def revoke_api_client(self, client_id: str) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE api_clients SET enabled=0,revoked_at=? WHERE id=?", (time.time(), client_id))
            self.db.commit()
            return bool(cur.rowcount)

    def list(self) -> list[DownloadTask]:
        with self._lock:
            return [self._row_to_task(row) for row in self.db.execute(
                "SELECT * FROM tasks ORDER BY queue_id, priority DESC, queue_order, created_at")]

    @staticmethod
    def _row_to_capture_batch(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "batch_id": row["batch_id"], "request_id": row["request_id"],
            "protocol_version": row["protocol_version"], "extension_origin": row["extension_origin"],
            "page_origin": row["page_origin"], "page_url": row["page_url"],
            "session_ref": row["session_ref"], "candidates": json.loads(row["candidates_json"] or "[]"),
            "state": row["state"], "acknowledged": bool(row["acknowledged"]),
            "imported_task_ids": json.loads(row["imported_task_ids_json"] or "[]"),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def save_capture_batch(self, batch: CaptureBatch | dict[str, Any]) -> dict[str, Any]:
        value = batch.to_dict() if isinstance(batch, CaptureBatch) else dict(batch)
        safe = self._strip_ephemeral(value)
        now = time.time()
        with self._lock:
            row = self.db.execute("SELECT * FROM capture_batches WHERE batch_id=? OR request_id=? LIMIT 1",
                                  (safe["batch_id"], safe["request_id"])).fetchone()
            if row:
                return self._row_to_capture_batch(row)
            self.db.execute("""INSERT INTO capture_batches
              (batch_id,request_id,protocol_version,extension_origin,page_origin,page_url,session_ref,
               candidates_json,state,acknowledged,imported_task_ids_json,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (safe["batch_id"], safe["request_id"], safe["protocol_version"], safe.get("extension_origin", ""),
               safe.get("page_origin", ""), safe.get("page_url"), safe.get("session_ref"),
               json.dumps(safe.get("candidates", [])), safe.get("state", "pending"),
               int(bool(safe.get("acknowledged", False))), json.dumps(safe.get("imported_task_ids", [])),
               float(safe.get("created_at", now)), now))
            self.db.commit()
            row = self.db.execute("SELECT * FROM capture_batches WHERE batch_id=?", (safe["batch_id"],)).fetchone()
        return self._row_to_capture_batch(row)

    def get_capture_batch(self, batch_id: str | None = None, request_id: str | None = None) -> dict[str, Any] | None:
        if not batch_id and not request_id:
            raise ValueError("batch_id or request_id is required")
        with self._lock:
            if batch_id:
                row = self.db.execute("SELECT * FROM capture_batches WHERE batch_id=?", (batch_id,)).fetchone()
            else:
                row = self.db.execute("SELECT * FROM capture_batches WHERE request_id=?", (request_id,)).fetchone()
        return self._row_to_capture_batch(row) if row else None

    def list_capture_batches(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM capture_batches ORDER BY updated_at DESC LIMIT ?",
                                   (max(1, min(int(limit), 500)),)).fetchall()
        return [self._row_to_capture_batch(row) for row in rows]

    def update_capture_batch(self, batch_id: str, *, state: str | None = None,
                             acknowledged: bool | None = None,
                             imported_task_ids: list[str] | None = None) -> dict[str, Any] | None:
        current = self.get_capture_batch(batch_id)
        if current is None:
            return None
        if state is not None:
            current["state"] = state
        if acknowledged is not None:
            current["acknowledged"] = bool(acknowledged)
        if imported_task_ids is not None:
            current["imported_task_ids"] = sorted(set(imported_task_ids))
        with self._lock:
            self.db.execute("""UPDATE capture_batches SET state=?,acknowledged=?,imported_task_ids_json=?,updated_at=?
                              WHERE batch_id=?""",
                            (current["state"], int(current["acknowledged"]),
                             json.dumps(current["imported_task_ids"]), time.time(), batch_id))
            self.db.commit()
        return self.get_capture_batch(batch_id)

    def find_nonterminal_task_by_fingerprint(self, fingerprint: str) -> DownloadTask | None:
        with self._lock:
            row = self.db.execute("""SELECT * FROM tasks WHERE source_fingerprint=?
              AND state NOT IN ('completed','canceled','failed') ORDER BY created_at LIMIT 1""",
                                 (fingerprint,)).fetchone()
        return self._row_to_task(row) if row else None

    def save_queue(self, queue: DownloadQueue | dict[str, Any]) -> None:
        value = queue.to_dict() if isinstance(queue, DownloadQueue) else queue
        now = time.time()
        with self._lock:
            self.db.execute("""INSERT INTO queues
              (id,name,enabled,paused,max_active,start_hour,end_hour,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
              enabled=excluded.enabled,paused=excluded.paused,max_active=excluded.max_active,
              start_hour=excluded.start_hour,end_hour=excluded.end_hour,updated_at=excluded.updated_at""",
              (value["id"], value.get("name", value["id"]), int(value.get("enabled", True)),
               int(value.get("paused", False)), value.get("max_active"), value.get("start_hour"),
               value.get("end_hour"), value.get("created_at") or now, now))
            self.db.commit()

    def list_queues(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) | {"enabled": bool(row["enabled"]), "paused": bool(row["paused"])}
                    for row in self.db.execute("SELECT * FROM queues ORDER BY id")]

    def get_queue(self, queue_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM queues WHERE id=?", (queue_id,)).fetchone()
        return (dict(row) | {"enabled": bool(row["enabled"]), "paused": bool(row["paused"])}) if row else None

    def delete_queue(self, queue_id: str) -> None:
        if queue_id == "default":
            raise ValueError("the default queue cannot be deleted")
        with self._lock:
            self.db.execute("UPDATE tasks SET queue_id='default' WHERE queue_id=?", (queue_id,))
            self.db.execute("DELETE FROM queues WHERE id=?", (queue_id,))
            self.db.commit()

    def save_hash(self, task_id: str, item_id: str, algorithm: str, digest: str, path: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO file_hashes(task_id,item_id,algorithm,digest,path,created_at) VALUES (?,?,?,?,?,?)",
                            (task_id, item_id, algorithm, digest, path, time.time()))
            self.db.commit()

    def list_hashes(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM file_hashes WHERE task_id=? ORDER BY item_id", (task_id,))]

    def save_shortlink_hop(self, task_id: str, hop: int, url: str, state: str,
                           provider: str | None = None, error: str | None = None) -> None:
        import hashlib
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        with self._lock:
            self.db.execute("""INSERT INTO shortlink_chains(task_id,hop,url_hash,host,provider,state,error,updated_at)
              VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(task_id,hop) DO UPDATE SET url_hash=excluded.url_hash,
              host=excluded.host,provider=excluded.provider,state=excluded.state,error=excluded.error,updated_at=excluded.updated_at""",
              (task_id, hop, hashlib.sha256(url.encode("utf-8")).hexdigest(), parsed.hostname or "",
               provider, state, error[:500] if error else None, time.time()))
            self.db.commit()

    def list_shortlink_chain(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM shortlink_chains WHERE task_id=? ORDER BY hop", (task_id,))]

    def record_diagnostic(self, task_id: str | None, level: str, stage: str,
                          message: str, details: dict[str, Any] | None = None) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO diagnostic_logs(task_id,level,stage,message,details_json,created_at) VALUES (?,?,?,?,?,?)",
                (task_id, level, stage, message[:1000], json.dumps(details or {}), time.time()))
            self.db.commit()
            return int(cur.lastrowid)

    def list_diagnostics(self, task_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM diagnostic_logs WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, max(1, min(int(limit), 2000))))
            return [{**dict(row), "details": json.loads(row["details_json"] or "{}")} for row in rows]

    def save_item_progress(self, task_id: str, item_id: str, completed_bytes: int, state: str) -> None:
        with self._lock:
            self.db.execute("UPDATE task_items SET completed_bytes=?,state=?,updated_at=? WHERE task_id=? AND item_id=?",
                            (completed_bytes, state, time.time(), task_id, item_id))
            self.db.commit()

    def save_account(self, account: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self.db.execute("""INSERT INTO provider_accounts
              (id,provider_id,label,credential_ref,enabled,state,expires_at,quota_bytes,used_bytes,last_error,updated_at,config_json,
               account_type,last_checked_at,quota_reset_at,health_json,refresh_state,scope_json,quarantine_until,priority)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET provider_id=excluded.provider_id,
              label=excluded.label,credential_ref=excluded.credential_ref,enabled=excluded.enabled,state=excluded.state,
              expires_at=excluded.expires_at,quota_bytes=excluded.quota_bytes,used_bytes=excluded.used_bytes,
              last_error=excluded.last_error,updated_at=excluded.updated_at,config_json=excluded.config_json,
              account_type=excluded.account_type,last_checked_at=excluded.last_checked_at,
              quota_reset_at=excluded.quota_reset_at,health_json=excluded.health_json,
              refresh_state=excluded.refresh_state,scope_json=excluded.scope_json,
              quarantine_until=excluded.quarantine_until,priority=excluded.priority""",
              (account["id"], account["provider_id"], account.get("label", account["id"]), account["credential_ref"],
               int(account.get("enabled", True)), account.get("state", "unknown"), account.get("expires_at"),
               account.get("quota_bytes"), account.get("used_bytes", 0), account.get("last_error"), now,
               json.dumps(account.get("config", {})), account.get("account_type", "credential"),
               account.get("last_checked_at"), account.get("quota_reset_at"), json.dumps(account.get("health", {})),
               account.get("refresh_state", "unknown"), json.dumps(account.get("scope", [])),
               account.get("quarantine_until"), int(account.get("priority", 0))))
            self.db.commit()

    def list_accounts(self, provider_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            query = "SELECT * FROM provider_accounts"
            args: tuple[Any, ...] = ()
            if provider_id:
                query += " WHERE provider_id=?"
                args = (provider_id,)
            result = []
            for row in self.db.execute(query + " ORDER BY provider_id,id", args):
                value = dict(row)
                value["enabled"] = bool(value["enabled"])
                value["config"] = json.loads(value.pop("config_json") or "{}")
                value["health"] = json.loads(value.pop("health_json") or "{}")
                value["scope"] = json.loads(value.pop("scope_json") or "[]")
                value["enabled"] = bool(value["enabled"])
                result.append(value)
            return result

    def update_account_health(self, account_id: str, state: str, error: str | None = None,
                              health: dict[str, Any] | None = None, checked_at: float | None = None) -> None:
        with self._lock:
            self.db.execute("""UPDATE provider_accounts SET state=?,last_error=?,last_checked_at=?,health_json=?,updated_at=?
                              WHERE id=?""", (state, error, checked_at or time.time(),
                                                json.dumps(health or {}), time.time(), account_id))
            self.db.commit()

    def update_account_refresh(self, account_id: str, refresh_state: str,
                               *, quarantine_until: float | None = None,
                               error: str | None = None) -> None:
        with self._lock:
            self.db.execute("""UPDATE provider_accounts SET refresh_state=?,quarantine_until=?,last_error=?,updated_at=?
                              WHERE id=?""", (refresh_state, quarantine_until, error, time.time(), account_id))
            self.db.commit()

    def delete_account(self, account_id: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM provider_accounts WHERE id=?", (account_id,))
            self.db.commit()

    def save_link(self, value: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self.db.execute("""INSERT INTO link_grabber
              (id,url,normalized_url,source,source_context,state,provider_id,title,error,selected,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(normalized_url) DO UPDATE SET source=excluded.source,
              source_context=excluded.source_context,title=COALESCE(excluded.title,link_grabber.title),
              updated_at=excluded.updated_at""",
              (value["id"], value["url"], value["normalized_url"], value.get("source", "clipboard"),
               value.get("source_context"), value.get("state", "pending"), value.get("provider_id"),
               value.get("title"), value.get("error"), int(value.get("selected", False)),
               value.get("created_at") or now, now))
            self.db.commit()
            row = self.db.execute("SELECT * FROM link_grabber WHERE normalized_url=?", (value["normalized_url"],)).fetchone()
        return dict(row) | {"selected": bool(row["selected"])}

    def list_links(self, state: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sql, args = "SELECT * FROM link_grabber", []
            clauses = []
            if state:
                clauses.append("state=?"); args.append(state)
            if query:
                clauses.append("(url LIKE ? OR title LIKE ? OR provider_id LIKE ?)")
                term = f"%{query}%"; args.extend([term, term, term])
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC"
            rows = [dict(row) for row in self.db.execute(sql, args)]
        # Appended field: what the link points at, by the engine's classifier.
        return [row | {"selected": bool(row["selected"]),
                       "file_kind": classify_file(filename=row.get("title"), source_url=row.get("normalized_url") or row.get("url")).category}
                for row in rows]

    def update_link(self, link_id: str, **values: Any) -> dict[str, Any] | None:
        allowed = {key: values[key] for key in ("state", "provider_id", "title", "error", "selected") if key in values}
        if not allowed:
            return next((row for row in self.list_links() if row["id"] == link_id), None)
        with self._lock:
            assignments = ", ".join(f"{key}=?" for key in allowed)
            params = [int(v) if key == "selected" else v for key, v in allowed.items()]
            params.extend([time.time(), link_id])
            self.db.execute(f"UPDATE link_grabber SET {assignments},updated_at=? WHERE id=?", params)
            self.db.commit()
        return next((row for row in self.list_links() if row["id"] == link_id), None)

    def bulk_update_links(self, link_ids: list[str], **values: Any) -> int:
        allowed = {key: values[key] for key in ("state", "selected", "title", "error") if key in values}
        if not allowed or not link_ids:
            return 0
        assignments = ", ".join(f"{key}=?" for key in allowed)
        params = [int(value) if key == "selected" else value for key, value in allowed.items()]
        params.append(time.time())
        with self._lock:
            cur = self.db.execute(
                f"UPDATE link_grabber SET {assignments},updated_at=? WHERE id IN ({','.join('?' for _ in link_ids)})",
                params + list(link_ids))
            self.db.commit()
            return int(cur.rowcount)

    def delete_link(self, link_id: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM link_grabber WHERE id=?", (link_id,))
            self.db.commit()

    def save_bandwidth_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        scope = str(profile.get("scope", "global")).strip().lower()
        if scope not in {"global", "queue", "provider", "account", "task"}:
            raise ValueError("bandwidth profile scope must be global, queue, provider, account, or task")
        scope_key = profile.get("scope_key")
        if scope != "global" and not str(scope_key or "").strip():
            raise ValueError(f"bandwidth profile scope {scope} requires scope_key")
        rate = normalize_bandwidth_rate(profile.get("rate_bytes_per_second", 0))
        with self._lock:
            self.db.execute("""INSERT INTO bandwidth_profiles
              (id,name,scope,scope_key,rate_bytes_per_second,windows_json,enabled,updated_at)
              VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,scope=excluded.scope,
              scope_key=excluded.scope_key,rate_bytes_per_second=excluded.rate_bytes_per_second,
              windows_json=excluded.windows_json,enabled=excluded.enabled,updated_at=excluded.updated_at""",
              (profile["id"], profile.get("name", profile["id"]), scope, scope_key,
               rate, json.dumps(profile.get("windows", [])),
               int(profile.get("enabled", True)), time.time()))
            self.db.commit()
        return next(row for row in self.list_bandwidth_profiles() if row["id"] == profile["id"])

    def list_bandwidth_profiles(self, scope: str | None = None, scope_key: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sql, args = "SELECT * FROM bandwidth_profiles", []
            clauses = []
            if scope:
                clauses.append("scope=?"); args.append(scope)
            if scope_key is not None:
                clauses.append("scope_key=?"); args.append(scope_key)
            if clauses: sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY scope,name"
            return [{**dict(row), "enabled": bool(row["enabled"]), "windows": json.loads(row["windows_json"] or "[]")} for row in self.db.execute(sql, args)]

    def delete_bandwidth_profile(self, profile_id: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM bandwidth_profiles WHERE id=?", (profile_id,))
            self.db.commit()

    def save_plugin_health(self, plugin_id: str, version: str, **values: Any) -> None:
        now = time.time()
        with self._lock:
            self.db.execute("""INSERT INTO plugin_health(plugin_id,version,enabled,quarantined,failure_count,last_error,updated_at)
              VALUES (?,?,?,?,?,?,?) ON CONFLICT(plugin_id) DO UPDATE SET version=excluded.version,
              enabled=excluded.enabled,quarantined=excluded.quarantined,failure_count=excluded.failure_count,
              last_error=excluded.last_error,updated_at=excluded.updated_at""",
              (plugin_id, version, int(values.get("enabled", True)), int(values.get("quarantined", False)),
               int(values.get("failure_count", 0)), values.get("last_error"), now))
            self.db.commit()

    def list_plugin_health(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) | {"enabled": bool(row["enabled"]), "quarantined": bool(row["quarantined"])}
                    for row in self.db.execute("SELECT * FROM plugin_health ORDER BY plugin_id")]

    def mark_plugin_health(self, plugin_id: str, version: str, **values: Any) -> None:
        self.save_plugin_health(plugin_id, version, **values)

    def save_notification_sink(self, sink: dict[str, Any]) -> None:
        with self._lock:
            self.db.execute("""INSERT INTO notification_sinks(id,kind,endpoint,credential_ref,enabled,event_types_json,updated_at)
              VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,endpoint=excluded.endpoint,
              credential_ref=excluded.credential_ref,enabled=excluded.enabled,event_types_json=excluded.event_types_json,
              updated_at=excluded.updated_at""",
              (sink["id"], sink["kind"], sink["endpoint"], sink.get("credential_ref"), int(sink.get("enabled", True)),
               json.dumps(sink.get("event_types", [])), time.time()))
            self.db.commit()

    def list_notification_sinks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{**dict(row), "enabled": bool(row["enabled"]), "event_types": json.loads(row["event_types_json"] or "[]")}
                    for row in self.db.execute("SELECT * FROM notification_sinks ORDER BY id")]

    def delete_notification_sink(self, sink_id: str) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM notification_sinks WHERE id=?", (sink_id,))
            self.db.commit()
            return cur.rowcount > 0

    def pending_deliveries(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("""SELECT e.*,s.id AS sink_id,s.kind,s.endpoint,s.credential_ref,s.event_types_json
              FROM event_outbox e JOIN notification_sinks s ON s.enabled=1
              LEFT JOIN event_deliveries d ON d.event_id=e.id AND d.sink_id=s.id
              WHERE e.published_at IS NULL AND (d.state IS NULL OR d.state='pending' OR (d.state='failed' AND d.attempts < 5))
              ORDER BY e.id LIMIT ?""", (limit,))
            return [{**dict(row), "payload": json.loads(row["payload_json"]),
                     "event_types": json.loads(row["event_types_json"] or "[]")} for row in rows]

    def record_delivery(self, event_id: int, sink_id: str, state: str, error: str | None = None) -> None:
        with self._lock:
            self.db.execute("""INSERT INTO event_deliveries(event_id,sink_id,attempts,state,last_error,delivered_at)
              VALUES (?,?,?,?,?,?) ON CONFLICT(event_id,sink_id) DO UPDATE SET attempts=event_deliveries.attempts+1,
              state=excluded.state,last_error=excluded.last_error,delivered_at=excluded.delivered_at""",
              (event_id, sink_id, 1, state, error, time.time() if state == "delivered" else None))
            self.db.commit()

    def event_has_pending_delivery(self, event_id: int) -> bool:
        with self._lock:
            return self.db.execute("""SELECT 1 FROM event_deliveries d
              JOIN notification_sinks s ON s.id=d.sink_id AND s.enabled=1
              WHERE d.event_id=? AND d.state <> 'delivered' LIMIT 1""", (event_id,)).fetchone() is not None

    def get(self, task_id: str) -> DownloadTask | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list_by_folder(self, folder_path: str) -> list["DownloadTask"]:
        """Return all tasks whose folder_path matches, used for multipart sibling-resume."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM tasks WHERE folder_path = ?", (folder_path,)
            ).fetchall()
        return [self._row_to_task(r) for r in rows if r]

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._deleted_task_ids.add(str(task_id))
            self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self.db.commit()

    def save_plan(self, task_id: str, provider: str | None, destination: str, expected_bytes: int,
                  policy_snapshot: dict[str, Any], credentials_ref: str | None,
                  plan_json: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self.db.execute("""INSERT INTO task_plans
              (task_id,provider,destination,expected_bytes,policy_json,credentials_ref,plan_json,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?)
              ON CONFLICT(task_id) DO UPDATE SET provider=excluded.provider, destination=excluded.destination,
              expected_bytes=excluded.expected_bytes, policy_json=excluded.policy_json,
              credentials_ref=excluded.credentials_ref, plan_json=excluded.plan_json, updated_at=excluded.updated_at""",
              (task_id, provider, destination, expected_bytes, json.dumps(policy_snapshot), credentials_ref,
               json.dumps(plan_json), now, now))
            self.db.commit()

    def save_segment(self, task_id: str, item_index: int, segment_index: int, start_byte: int,
                     end_byte: int | None, completed: bool, state: str, completed_bytes: int = 0,
                     validator: str | None = None) -> None:
        with self._lock:
            self.db.execute("""INSERT INTO segments
              (task_id,item_index,segment_index,start_byte,end_byte,completed,completed_bytes,validator,state,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(task_id,item_index,segment_index) DO UPDATE SET start_byte=excluded.start_byte,
              end_byte=excluded.end_byte, completed=excluded.completed, completed_bytes=excluded.completed_bytes,
              validator=excluded.validator, state=excluded.state,
              updated_at=excluded.updated_at""",
              (task_id, item_index, segment_index, start_byte, end_byte, int(completed), completed_bytes,
               validator, state, time.time()))
            self.db.commit()

    def list_segments(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM segments WHERE task_id=? ORDER BY item_index, segment_index", (task_id,))]

    def save_resolution_cache(self, context: ResolutionContext, items: list[ResolvedItem]) -> None:
        """Persist only source identity and metadata; never direct URLs or headers."""
        import hashlib
        key = hashlib.sha256(f"{context.source_url}\0{context.item_id or ''}".encode()).hexdigest()
        metadata = {"items": [self._strip_ephemeral({"provider": item.provider, "source_url": item.source_url,
                                "display_name": item.display_name, "relative_path": item.relative_path,
                                "size": item.size, "checksum": item.checksum, "metadata": item.metadata,
                                "item_id": item.item_id, "parent_id": item.parent_id,
                                "page": item.page, "cursor": item.cursor,
                                "folder_path": item.folder_path, "package_path": item.package_path}) for item in items]}
        with self._lock:
            self.db.execute("""INSERT INTO resolution_cache(cache_key,source_url,provider_id,item_id,metadata_json,expires_at,refresh_policy,updated_at)
              VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET provider_id=excluded.provider_id,
              metadata_json=excluded.metadata_json,expires_at=excluded.expires_at,refresh_policy=excluded.refresh_policy,
              updated_at=excluded.updated_at""", (key, context.source_url, context.provider_id, context.item_id,
                                                    json.dumps(metadata), context.expires_at, context.refresh_policy, time.time()))
            self.db.commit()

    def get_resolution_cache(self, source_url: str, item_id: str | None = None) -> dict[str, Any] | None:
        import hashlib
        key = hashlib.sha256(f"{source_url}\0{item_id or ''}".encode()).hexdigest()
        with self._lock:
            row = self.db.execute("SELECT * FROM resolution_cache WHERE cache_key=?", (key,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
        return value

    def save_collection_plan(self, plan: CollectionPlan) -> None:
        with self._lock:
            self.db.execute("""INSERT INTO collection_plans(id,source_url,provider_id,page,cursor,has_more,state,plan_json,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET page=excluded.page,cursor=excluded.cursor,
              has_more=excluded.has_more,state=excluded.state,plan_json=excluded.plan_json,updated_at=excluded.updated_at""",
              (plan.id, plan.source_url, plan.provider_id, plan.page, plan.cursor, int(plan.has_more), plan.state,
               json.dumps(plan.to_dict()), time.time()))
            self.db.execute("DELETE FROM crawl_nodes WHERE crawl_id=?", (plan.id,))
            self.db.execute("DELETE FROM crawl_events WHERE crawl_id=?", (plan.id,))
            for node in plan.nodes:
                safe = json.loads(json.dumps(node))
                self.db.execute("""INSERT INTO crawl_nodes(crawl_id,node_id,parent_id,depth,source_url,canonical_url,page,cursor,
                  folder_path,package_path,status,outcome,node_json,revision,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (plan.id, safe["node_id"], safe.get("parent_id"), int(safe.get("depth", 0)), safe["source_url"], safe.get("canonical_url", safe["source_url"]),
                   int(safe.get("page", 0)), safe.get("cursor"), safe.get("folder_path", ""), safe.get("package_path", ""),
                   safe.get("status", "discovered"), safe.get("outcome"), json.dumps(safe), int(plan.graph_revision), time.time()))
            for event in plan.events:
                self.db.execute("INSERT INTO crawl_events(crawl_id,revision,outcome,payload_json,created_at) VALUES (?,?,?,?,?)",
                                (plan.id, int(plan.graph_revision), str(event.get("outcome", "info")), json.dumps(event), time.time()))
            self.db.commit()

    def list_crawl_nodes(self, crawl_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(row[0]) for row in self.db.execute(
                "SELECT node_json FROM crawl_nodes WHERE crawl_id=? ORDER BY rowid", (crawl_id,))]

    def list_crawl_events(self, crawl_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT revision,outcome,payload_json,created_at FROM crawl_events WHERE crawl_id=? ORDER BY id", (crawl_id,))
            return [{"revision": row[0], "outcome": row[1], **json.loads(row[2]), "created_at": row[3]} for row in rows]

    def get_collection_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT plan_json FROM collection_plans WHERE id=?", (plan_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_collection_plans(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT plan_json FROM collection_plans ORDER BY updated_at DESC")]

    def save_provider_health_snapshot(self, snapshot: ProviderHealthSnapshot) -> None:
        with self._lock:
            self.db.execute("""INSERT INTO provider_health_snapshots(provider_id,success_rate,total_attempts,successes,
              failure_categories_json,last_known_version,quarantined,last_error,checked_at) VALUES (?,?,?,?,?,?,?,?,?)
              ON CONFLICT(provider_id) DO UPDATE SET success_rate=excluded.success_rate,total_attempts=excluded.total_attempts,
              successes=excluded.successes,failure_categories_json=excluded.failure_categories_json,last_known_version=excluded.last_known_version,
              quarantined=excluded.quarantined,last_error=excluded.last_error,checked_at=excluded.checked_at""",
              (snapshot.provider_id, snapshot.success_rate, snapshot.total_attempts, snapshot.successes,
               json.dumps(snapshot.failure_categories), snapshot.last_known_version, int(snapshot.quarantined),
               snapshot.last_error, snapshot.checked_at))
            self.db.commit()

    def list_provider_health_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for row in self.db.execute("SELECT * FROM provider_health_snapshots ORDER BY provider_id"):
                value = dict(row)
                value["failure_categories"] = json.loads(value.pop("failure_categories_json") or "{}")
                value["quarantined"] = bool(value["quarantined"])
                result.append(value)
            return result

    def create_lease(self, lease: TransferLease) -> None:
        with self._lock:
            self.db.execute("""INSERT OR REPLACE INTO transfer_leases
              (lease_id,task_id,host,provider,account_ref,segment_count,expires_at,released_at,created_at)
              VALUES (?,?,?,?,?,?,?,?,?)""",
              (lease.lease_id, lease.task_id, lease.host, lease.provider, lease.account_ref,
               lease.segment_count, lease.expires_at, lease.released_at, time.time()))
            self.db.commit()

    def release_lease(self, lease_id: str) -> None:
        with self._lock:
            self.db.execute("UPDATE transfer_leases SET released_at=? WHERE lease_id=?",
                            (time.time(), lease_id))
            self.db.commit()

    def release_expired_leases(self, now: float | None = None) -> int:
        with self._lock:
            cur = self.db.execute(
                "UPDATE transfer_leases SET released_at=? WHERE released_at IS NULL AND expires_at<=?",
                (time.time(), now if now is not None else time.time()))
            self.db.commit()
            return cur.rowcount

    def list_leases(self, active_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM transfer_leases"
        args: tuple[Any, ...] = ()
        if active_only:
            query += " WHERE released_at IS NULL AND expires_at>?"
            args = (time.time(),)
        with self._lock:
            return [dict(row) for row in self.db.execute(query, args)]

    def record_retry(self, task_id: str, category: str, error: str,
                     item_index: int | None = None, status_code: int | None = None,
                     delay_seconds: float = 0) -> None:
        with self._lock:
            self.db.execute("""INSERT INTO retry_attempts
              (task_id,item_index,category,status_code,delay_seconds,error,created_at)
              VALUES (?,?,?,?,?,?,?)""",
              (task_id, item_index, category, status_code, delay_seconds, error, time.time()))
            self.db.commit()

    def reserve_daily_quota(self, amount: int, daily_limit: int, usage_day: str | None = None) -> bool:
        """Atomically reserve expected bytes; a zero limit means unlimited."""
        if amount <= 0 or daily_limit <= 0:
            return True
        day = usage_day or time.strftime("%Y-%m-%d", time.localtime())
        with self._lock:
            row = self.db.execute("SELECT bytes_reserved FROM quota_usage WHERE usage_day=?", (day,)).fetchone()
            used = int(row[0]) if row else 0
            if used + amount > daily_limit:
                return False
            self.db.execute("INSERT INTO quota_usage(usage_day,bytes_reserved) VALUES (?,?) "
                            "ON CONFLICT(usage_day) DO UPDATE SET bytes_reserved=bytes_reserved+excluded.bytes_reserved",
                            (day, amount))
            self.db.commit()
            return True

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.db.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self.db.execute("INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                            (key, json.dumps(value), time.time()))
            self.db.commit()

    def record_provider_attempt(self, task_id: str, source_fingerprint: str, provider_id: str,
                                route_profile_id: str, outcome: str, status_code: int | None = None,
                                retry_after: float | None = None, error: str | None = None) -> None:
        with self._lock:
            self.db.execute("""INSERT INTO provider_attempts
              (task_id,source_fingerprint,provider_id,route_profile_id,outcome,status_code,retry_after,started_at,ended_at,error)
              VALUES (?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(task_id,source_fingerprint,provider_id,route_profile_id) DO UPDATE SET
              outcome=excluded.outcome,status_code=excluded.status_code,retry_after=excluded.retry_after,
              ended_at=excluded.ended_at,error=excluded.error""",
              (task_id, source_fingerprint, provider_id, route_profile_id, outcome, status_code,
               retry_after, time.time(), time.time(), error))
            self.db.commit()

    def list_provider_attempts(self, task_id: str, source_fingerprint: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            query = "SELECT * FROM provider_attempts WHERE task_id=?"
            args: tuple[Any, ...] = (task_id,)
            if source_fingerprint:
                query += " AND source_fingerprint=?"
                args += (source_fingerprint,)
            return [dict(row) for row in self.db.execute(query, args)]

    def save_route_profile(self, profile: dict[str, Any]) -> None:
        with self._lock:
            self.db.execute("""INSERT INTO route_profiles(id,kind,endpoint,credential_ref,region,healthcheck_url,enabled,config_json)
              VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,endpoint=excluded.endpoint,
              credential_ref=excluded.credential_ref,region=excluded.region,healthcheck_url=excluded.healthcheck_url,
              enabled=excluded.enabled,config_json=excluded.config_json""",
              (profile["id"], profile.get("kind", "direct"), profile.get("endpoint"), profile.get("credential_ref"),
               profile.get("region"), profile.get("healthcheck_url"), int(profile.get("enabled", True)), json.dumps(profile)))
            self.db.commit()

    def list_route_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            profiles = []
            for row in self.db.execute("SELECT id, config_json FROM route_profiles ORDER BY id"):
                try:
                    data = json.loads(row[1]) if row[1] else {}
                except Exception:
                    data = {}
                if not data.get("id"):
                    data["id"] = row[0]
                profiles.append(data)
            return profiles

    def delete_route_profile(self, profile_id: str) -> bool:
        with self._lock:
            self.db.execute("DELETE FROM route_profiles WHERE id = ?", (profile_id,))
            self.db.commit()
            return True

    def record_route_attempt(self, task_id: str, profile_id: str, outcome: str, reason: str | None = None) -> int:
        with self._lock:
            cur = self.db.execute("INSERT INTO route_attempts(task_id,profile_id,outcome,reason,started_at,ended_at) VALUES (?,?,?,?,?,?)",
                                  (task_id, profile_id, outcome, reason, time.time(), time.time()))
            self.db.commit()
            return int(cur.lastrowid)

    def list_route_attempts(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM route_attempts WHERE task_id=? ORDER BY id", (task_id,))]

    def save_archive_job(self, job: ArchiveJob) -> None:
        with self._lock:
            now = time.time()
            self.db.execute("""INSERT INTO archive_jobs
              (id,task_id,input_path,output_directory,format,password_ref,policy_json,state,
               progress_bytes,error,created_at,updated_at,package_key,operation,part_manifest_json,
               expected_size,observed_size,expected_digest,observed_digest,attempt,recovery_reason,revision,staging_path)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET state=excluded.state,
              progress_bytes=excluded.progress_bytes, error=excluded.error, updated_at=excluded.updated_at,
              package_key=excluded.package_key, operation=excluded.operation, part_manifest_json=excluded.part_manifest_json,
              expected_size=excluded.expected_size, observed_size=excluded.observed_size,
              expected_digest=excluded.expected_digest, observed_digest=excluded.observed_digest,
              attempt=excluded.attempt, recovery_reason=excluded.recovery_reason,
              revision=excluded.revision, staging_path=excluded.staging_path""",
              (job.id, job.task_id, job.input_path, job.output_directory, job.format, job.password_ref,
               json.dumps(job.policy), job.state, job.progress_bytes, job.error, now, now,
               job.package_key, job.operation, json.dumps(job.part_manifest), job.expected_size,
               job.observed_size, job.expected_digest, job.observed_digest, job.attempt,
               job.recovery_reason, job.revision, job.staging_path))
            self.db.commit()

    def insert_or_get_archive_job(self, job: ArchiveJob) -> tuple[ArchiveJob, bool]:
        """Atomically create a package job once, returning the existing job on replay."""
        if not job.package_key:
            raise ValueError("archive package_key is required")
        with self._lock:
            now = time.time()
            self.db.execute("""INSERT OR IGNORE INTO archive_jobs
              (id,task_id,input_path,output_directory,format,password_ref,policy_json,state,
               progress_bytes,error,created_at,updated_at,package_key,operation,part_manifest_json,
               expected_size,observed_size,expected_digest,observed_digest,attempt,recovery_reason,revision,staging_path)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (job.id, job.task_id, job.input_path, job.output_directory, job.format, job.password_ref,
               json.dumps(job.policy), job.state, job.progress_bytes, job.error, now, now,
               job.package_key, job.operation, json.dumps(job.part_manifest), job.expected_size,
               job.observed_size, job.expected_digest, job.observed_digest, job.attempt,
               job.recovery_reason, job.revision, job.staging_path))
            row = self.db.execute("SELECT * FROM archive_jobs WHERE package_key=?", (job.package_key,)).fetchone()
            self.db.commit()
        if not row:
            raise RuntimeError("archive job insert failed")
        return self._row_to_archive_job(row), str(row["id"]) == job.id

    @staticmethod
    def _row_to_archive_job(row: sqlite3.Row) -> ArchiveJob:
        return ArchiveJob(
            row["id"], row["task_id"], row["input_path"], row["output_directory"], row["format"],
            row["password_ref"], json.loads(row["policy_json"] or "{}"), row["state"],
            int(row["progress_bytes"] or 0), row["error"], row["package_key"], row["operation"] or "join_verify",
            json.loads(row["part_manifest_json"] or "[]"), row["expected_size"], row["observed_size"],
            row["expected_digest"], row["observed_digest"], int(row["attempt"] or 0),
            row["recovery_reason"], int(row["revision"] or 0), row["staging_path"])

    def transition_archive_job(self, job_id: str, state: str, *, error: str | None = None,
                               expected_revision: int | None = None, recovery_reason: str | None = None) -> ArchiveJob:
        with self._lock:
            row = self.db.execute("SELECT * FROM archive_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("archive job not found")
            job = self._row_to_archive_job(row)
            if not job.can_transition(state):
                raise ValueError(f"illegal archive job transition {job.state} -> {state}")
            if expected_revision is not None and job.revision != int(expected_revision):
                raise RevisionConflict(f"archive job revision conflict: expected {expected_revision}, got {job.revision}")
            job.state = state
            job.error = error
            job.recovery_reason = recovery_reason if recovery_reason is not None else job.recovery_reason
            job.revision += 1
            job.attempt += 1 if state == "running" else 0
            now = time.time()
            self.db.execute("UPDATE archive_jobs SET state=?,error=?,recovery_reason=?,revision=?,attempt=?,updated_at=? WHERE id=?",
                            (job.state, job.error, job.recovery_reason, job.revision, job.attempt, now, job.id))
            self.db.commit()
            return job

    def update_archive_job(self, job: ArchiveJob, *, expected_revision: int | None = None) -> ArchiveJob:
        with self._lock:
            current = self.db.execute("SELECT revision FROM archive_jobs WHERE id=?", (job.id,)).fetchone()
            if not current:
                raise KeyError("archive job not found")
            actual = int(current["revision"] or 0)
            if expected_revision is not None and actual != int(expected_revision):
                raise RevisionConflict(f"archive job revision conflict: expected {expected_revision}, got {actual}")
            job.revision = actual + 1
            # Append-only persistence: the manifest, verified size/digest and resolved
            # output directory must survive a restart so recovery never re-derives them
            # from stale in-memory state.
            self.db.execute("""UPDATE archive_jobs SET progress_bytes=?,observed_size=?,observed_digest=?,
              error=?,recovery_reason=?,revision=?,staging_path=?,part_manifest_json=?,expected_size=?,
              output_directory=?,expected_digest=?,updated_at=? WHERE id=?""",
              (job.progress_bytes, job.observed_size, job.observed_digest, job.error,
               job.recovery_reason, job.revision, job.staging_path, json.dumps(job.part_manifest),
               job.expected_size, job.output_directory, job.expected_digest, time.time(), job.id))
            self.db.commit()
            return job

    def _normalize_archive_job_unlocked(self, job_id: str, *, recovery_reason: str | None) -> bool:
        """Single-statement normalization shared by boot recovery and RPC retry."""
        cursor = self.db.execute(
            """UPDATE archive_jobs SET state='queued',staging_path=NULL,
               recovery_reason=COALESCE(?,recovery_reason),revision=revision+1,updated_at=?
               WHERE id=? AND state IN (?,?,?,?)""",
            (recovery_reason, time.time(), job_id, *ARCHIVE_ACTIVE_STATES))
        return cursor.rowcount > 0

    def normalize_archive_job_for_retry(self, job_id: str) -> ArchiveJob:
        """Return an active job to queued so an explicit retry never hits the state guard.

        Live states (running/verifying/extracting/cleanup_pending) cannot legally
        transition back to queued; a retry against a job whose worker died would
        otherwise be rejected or leave a concurrent run marked failed.
        """
        with self._lock:
            row = self.db.execute("SELECT id FROM archive_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("archive job not found")
            self._normalize_archive_job_unlocked(job_id, recovery_reason=None)
            self.db.commit()
            refreshed = self.db.execute("SELECT * FROM archive_jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_archive_job(refreshed)

    def recover_archive_jobs(self) -> list[ArchiveJob]:
        with self._lock:
            rows = self.db.execute(
                "SELECT id FROM archive_jobs WHERE state IN (?,?,?,?)", ARCHIVE_ACTIVE_STATES).fetchall()
            job_ids = [row["id"] for row in rows]
            for job_id in job_ids:
                self._normalize_archive_job_unlocked(job_id, recovery_reason="application_restart")
            self.db.commit()
            jobs: list[ArchiveJob] = []
            for job_id in job_ids:
                refreshed = self.db.execute("SELECT * FROM archive_jobs WHERE id=?", (job_id,)).fetchone()
                if refreshed:
                    jobs.append(self._row_to_archive_job(refreshed))
            return jobs

    def list_archive_jobs_for_boot(self) -> list[ArchiveJob]:
        """Jobs that still need scheduling after a single recover_archive_jobs() pass.

        Boot must schedule from this list only; mixing it with a full
        list_archive_jobs() re-queues every recovered job a second time.
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM archive_jobs WHERE state='queued' ORDER BY created_at, id").fetchall()
            return [self._row_to_archive_job(row) for row in rows]

    def get_archive_job(self, job_id: str) -> ArchiveJob | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM archive_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        return self._row_to_archive_job(row)

    def list_archive_jobs(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            query = "SELECT * FROM archive_jobs"
            args: tuple[Any, ...] = ()
            if task_id:
                query += " WHERE task_id=?"
                args = (task_id,)
            return [dict(row) for row in self.db.execute(query, args)]

    def _enqueue_event_unlocked(self, event_type: str, task_id: str | None,
                                payload: dict[str, Any], dedupe_key: str | None) -> int:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO event_outbox(event_type,task_id,payload_json,dedupe_key,created_at) VALUES (?,?,?,?,?)",
            (event_type, task_id, json.dumps(payload), dedupe_key, time.time()))
        if cur.lastrowid:
            return int(cur.lastrowid)
        if dedupe_key:
            # A row with this dedupe_key already exists. If still unpublished,
            # update its payload so the consumer receives the latest snapshot rather
            # than the first-seen value (important for live progress ticks).
            self.db.execute(
                "UPDATE event_outbox SET payload_json=?, created_at=?"
                " WHERE dedupe_key=? AND published_at IS NULL",
                (json.dumps(payload), time.time(), dedupe_key))
            row = self.db.execute("SELECT id FROM event_outbox WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            return int(row["id"]) if row else 0
        return 0

    def enqueue_event(self, event_type: str, task_id: str | None, payload: dict[str, Any],
                      dedupe_key: str | None = None) -> int:
        with self._lock:
            event_id = self._enqueue_event_unlocked(event_type, task_id, payload, dedupe_key)
            self.db.commit()
            return event_id

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM event_outbox WHERE published_at IS NULL ORDER BY id LIMIT ?", (limit,))
            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def latest_event_id(self) -> int:
        """Highest outbox id currently persisted (0 when the outbox is empty)."""
        with self._lock:
            row = self.db.execute("SELECT COALESCE(MAX(id), 0) AS latest_id FROM event_outbox").fetchone()
            return int(row["latest_id"])

    def events_since(self, event_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM event_outbox WHERE id>? ORDER BY id LIMIT ?",
                (int(event_id), max(1, min(int(limit), 2000))))
            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def mark_event_published(self, event_id: int) -> None:
        with self._lock:
            self.db.execute("UPDATE event_outbox SET published_at=? WHERE id=?", (time.time(), event_id))
            self.db.commit()

    def prune_event_outbox(self, max_age_seconds: float = 86400.0, max_rows: int = 50000) -> int:
        """Remove acknowledged events older than the retention window (or above
        a row cap) plus orphaned rows whose task was deleted. Returns rows pruned."""
        with self._lock:
            cutoff = time.time() - max_age_seconds
            cursor = self.db.execute(
                "DELETE FROM event_outbox WHERE (published_at IS NOT NULL AND created_at < ?) "
                "OR id <= (SELECT COALESCE(MAX(id),0) - ? FROM event_outbox)",
                (cutoff, max_rows))
            orphan = self.db.execute(
                "DELETE FROM event_outbox WHERE task_id IS NOT NULL AND "
                "task_id NOT IN (SELECT id FROM tasks)")
            self.db.commit()
            return int(cursor.rowcount) + int(orphan.rowcount)

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> DownloadTask:
        return DownloadTask(
            id=row["id"], source_url=row["source_url"], destination=row["destination"],
            display_name=row["display_name"], password_ref=row["password_ref"], provider=row["provider"],
            state=row["state"], size=row["size"], completed_bytes=row["completed_bytes"],
            error=row["error"], retry_count=row["retry_count"],
            resolved=[ResolvedItem(**item) for item in json.loads(row["resolved_json"])],
            route_profile_id=row["route_profile_id"] if "route_profile_id" in row.keys() else None,
            alternate_urls=json.loads(row["alternates_json"]) if "alternates_json" in row.keys() else [],
            priority=row["priority"] if "priority" in row.keys() else 0,
            queue_id=row["queue_id"] if "queue_id" in row.keys() else "default",
            queue_order=row["queue_order"] if "queue_order" in row.keys() else 0,
            scheduled_at=row["scheduled_at"] if "scheduled_at" in row.keys() else None,
            paused_reason=row["paused_reason"] if "paused_reason" in row.keys() else None,
            category=row["category"] if "category" in row.keys() else None,
            duplicate_strategy=row["duplicate_strategy"] if "duplicate_strategy" in row.keys() else "skip",
            source_fingerprint=row["source_fingerprint"] if "source_fingerprint" in row.keys() else None,
            finished_at=row["finished_at"] if "finished_at" in row.keys() else None,
            user_action=row["user_action"] if "user_action" in row.keys() else None,
            user_challenge=json.loads(row["user_challenge_json"] or "{}") if "user_challenge_json" in row.keys() else {},
            request_headers=json.loads(row["request_headers_json"] or "{}") if "request_headers_json" in row.keys() else {},
            referrer=row["referrer"] if "referrer" in row.keys() else None,
            browser_context=json.loads(row["browser_context_json"] or "{}") if "browser_context_json" in row.keys() else {},
            selected_item_ids=json.loads(row["selection_json"] or "null") if "selection_json" in row.keys() else None,
            account_ref=row["account_ref"] if "account_ref" in row.keys() else None,
            revision=int(row["revision"] or 0) if "revision" in row.keys() else 0,
            lease_id=row["lease_id"] if "lease_id" in row.keys() else None,
            heartbeat_at=row["heartbeat_at"] if "heartbeat_at" in row.keys() else None,
            recovery_reason=row["recovery_reason"] if "recovery_reason" in row.keys() else None,
            integrity=json.loads(row["integrity_json"] or "{}") if "integrity_json" in row.keys() else {},
            folder_path=row["folder_path"] if "folder_path" in row.keys() else None,
            package_key=row["package_key"] if "package_key" in row.keys() else None,
            package_part_number=int(row["package_part_number"]) if "package_part_number" in row.keys() and row["package_part_number"] is not None else None,
            package_part_count=int(row["package_part_count"]) if "package_part_count" in row.keys() and row["package_part_count"] is not None else None,
            package_leader_id=row["package_leader_id"] if "package_leader_id" in row.keys() else None,
            speed_bytes_per_second=float(row["speed_bytes_per_second"] or 0) if "speed_bytes_per_second" in row.keys() else 0,
            average_speed_bytes_per_second=float(row["average_speed_bytes_per_second"] or 0) if "average_speed_bytes_per_second" in row.keys() else 0,
            eta_seconds=float(row["eta_seconds"]) if "eta_seconds" in row.keys() and row["eta_seconds"] is not None else None,
            backend=row["backend"] if "backend" in row.keys() else None,
            attempt_count=int(row["attempt_count"] or 0) if "attempt_count" in row.keys() else 0,
            started_at=float(row["started_at"]) if "started_at" in row.keys() and row["started_at"] is not None else None,
            telemetry_updated_at=float(row["telemetry_updated_at"]) if "telemetry_updated_at" in row.keys() and row["telemetry_updated_at"] is not None else None,
            integrity_state=row["integrity_state"] if "integrity_state" in row.keys() else "unverified",
            stage=row["stage"] if "stage" in row.keys() else None,
            stage_detail=json.loads(row["stage_detail_json"] or "{}") if "stage_detail_json" in row.keys() else {},
            stage_entered_at=float(row["stage_entered_at"]) if "stage_entered_at" in row.keys() and row["stage_entered_at"] is not None else None,
            stage_history=json.loads(row["stage_history_json"] or "[]") if "stage_history_json" in row.keys() else [])

    # History outlives the downloads list: removing a finished download (or
    # clearing completed ones) keeps its history row; only History's own Clear
    # removes it.
    _HISTORY_FIELDS = ("id", "source_url", "display_name", "state", "size", "completed_bytes", "provider",
                       "destination", "folder_path", "package_key", "error", "started_at", "finished_at",
                       "retry_count", "integrity_state", "category", "route_profile_id", "backend")

    def record_history(self, task: dict[str, Any]) -> None:
        record = {key: task.get(key) for key in self._HISTORY_FIELDS}
        with self._lock:
            self.db.execute("""INSERT INTO download_history(task_id, finished_at, record_json) VALUES (?,?,?)
              ON CONFLICT(task_id) DO UPDATE SET finished_at=excluded.finished_at, record_json=excluded.record_json""",
                            (record["id"], float(record.get("finished_at") or time.time()), json.dumps(record)))
            self.db.commit()

    def list_history(self, limit: int = 2000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT record_json FROM download_history ORDER BY finished_at DESC LIMIT ?",
                                   (int(limit),)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def clear_history(self, task_ids: list[str] | None = None) -> int:
        with self._lock:
            if task_ids is None:
                cursor = self.db.execute("DELETE FROM download_history")
            else:
                marks = ",".join("?" for _ in task_ids) or "''"
                cursor = self.db.execute(f"DELETE FROM download_history WHERE task_id IN ({marks})", list(task_ids))
            self.db.commit()
            return cursor.rowcount

    def save_captcha_challenge(self, challenge: Any) -> None:
        c_dict = challenge.to_dict() if hasattr(challenge, "to_dict") else dict(challenge)
        with self._lock:
            self.db.execute("""
            INSERT INTO captcha_challenges (
                id, task_id, provider_id, captcha_type, status, params_json, solution_json,
                solver_id, error, timeout_seconds, created_at, expires_at, resolved_at, retry_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                params_json=excluded.params_json,
                solution_json=excluded.solution_json,
                solver_id=excluded.solver_id,
                error=excluded.error,
                expires_at=excluded.expires_at,
                resolved_at=excluded.resolved_at,
                retry_count=excluded.retry_count
            """, (
                c_dict["id"], c_dict.get("task_id"), c_dict.get("provider_id", "generic"),
                c_dict.get("captcha_type", "image_text"), c_dict.get("status", "pending"),
                json.dumps(c_dict.get("params", {})),
                json.dumps(c_dict["solution"]) if c_dict.get("solution") is not None else None,
                c_dict.get("solver_used"), c_dict.get("error"),
                float(c_dict.get("timeout_seconds", 90.0)),
                float(c_dict.get("created_at", time.time())),
                float(c_dict.get("expires_at", time.time() + 90.0)),
                float(c_dict["resolved_at"]) if c_dict.get("resolved_at") is not None else None,
                int(c_dict.get("retry_count", 0)),
            ))
            self.db.commit()

    def get_captcha_challenge(self, challenge_id: str) -> Any | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM captcha_challenges WHERE id=?", (challenge_id,)).fetchone()
            if not row:
                return None
            from .captcha import CaptchaChallenge, CaptchaStatus, CaptchaType
            c_type = row["captcha_type"]
            try:
                c_type = CaptchaType(c_type)
            except ValueError:
                pass
            c_status = row["status"]
            try:
                c_status = CaptchaStatus(c_status)
            except ValueError:
                pass
            return CaptchaChallenge(
                id=row["id"],
                task_id=row["task_id"],
                provider_id=row["provider_id"],
                captcha_type=c_type,
                params=json.loads(row["params_json"] or "{}"),
                timeout_seconds=row["timeout_seconds"],
                created_at=row["created_at"],
                expires_at=row["expires_at"],
                status=c_status,
                solution=json.loads(row["solution_json"]) if row["solution_json"] else None,
                solver_used=row["solver_id"],
                error=row["error"],
                resolved_at=row["resolved_at"],
                retry_count=row["retry_count"],
            )

    def list_captcha_history(self, limit: int = 200) -> list[dict[str, Any]]:
        """Most recent challenges of every status, joined with their download's name."""
        with self._lock:
            rows = self.db.execute(
                """SELECT c.id, c.task_id, c.provider_id, c.captcha_type, c.status, c.params_json,
                          c.solver_id, c.error, c.created_at, c.expires_at, c.resolved_at, c.retry_count,
                          t.display_name AS task_name, t.source_url AS task_url
                   FROM captcha_challenges c LEFT JOIN tasks t ON t.id = c.task_id
                   ORDER BY c.created_at DESC LIMIT ?""",
                (max(1, min(int(limit), 1000)),)).fetchall()
            return [{**{key: row[key] for key in row.keys() if key != "params_json"},
                     "params": json.loads(row["params_json"] or "{}")} for row in rows]

    def list_recent_shortlink_chains(self, limit_tasks: int = 30) -> list[dict[str, Any]]:
        """Every hop for the downloads whose shortlink chains changed most recently."""
        with self._lock:
            rows = self.db.execute(
                """SELECT s.task_id, s.hop, s.host, s.provider, s.state, s.error, s.updated_at,
                          t.display_name AS task_name, t.source_url AS task_url, t.state AS task_state
                   FROM shortlink_chains s JOIN tasks t ON t.id = s.task_id
                   WHERE s.task_id IN (SELECT task_id FROM shortlink_chains GROUP BY task_id
                                       ORDER BY MAX(updated_at) DESC LIMIT ?)
                   ORDER BY s.task_id, s.hop""",
                (max(1, min(int(limit_tasks), 200)),)).fetchall()
            return [dict(row) for row in rows]

    def task_activity_counts(self, task_ids: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Captcha and shortlink-hop counts per task, for list badges."""
        ids = [str(task_id) for task_id in task_ids][:2000]
        if not ids:
            return [], []
        marks = ",".join("?" for _ in ids)
        with self._lock:
            captchas = [dict(row) for row in self.db.execute(
                f"""SELECT task_id, COUNT(*) AS total,
                           SUM(CASE WHEN status IN ('failed','expired') THEN 1 ELSE 0 END) AS failed
                    FROM captcha_challenges WHERE task_id IN ({marks}) GROUP BY task_id""", ids)]
            hops = [dict(row) for row in self.db.execute(
                f"SELECT task_id, COUNT(*) AS hops FROM shortlink_chains WHERE task_id IN ({marks}) GROUP BY task_id",
                ids)]
        return captchas, hops

    def list_captcha_challenges(self, task_id: str | None = None, pending_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            query = "SELECT * FROM captcha_challenges WHERE 1=1"
            params = []
            if task_id:
                query += " AND task_id=?"
                params.append(task_id)
            if pending_only:
                query += " AND status IN ('pending', 'solving')"
            query += " ORDER BY created_at DESC"
            rows = self.db.execute(query, tuple(params)).fetchall()
            return [{
                "id": r["id"],
                "task_id": r["task_id"],
                "provider_id": r["provider_id"],
                "captcha_type": r["captcha_type"],
                "status": r["status"],
                "params": json.loads(r["params_json"] or "{}"),
                "solution": json.loads(r["solution_json"]) if r["solution_json"] else None,
                "solver_used": r["solver_id"],
                "error": r["error"],
                "timeout_seconds": r["timeout_seconds"],
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
                "resolved_at": r["resolved_at"],
                "retry_count": r["retry_count"],
            } for r in rows]

    def save_host_concurrency_profile(
        self,
        host: str,
        verified_ceiling: int,
        denied_at_ceiling: int | None,
        cooldown_until: float,
        last_probe_at: float,
        rejection_reasons: list[str],
    ) -> None:
        clean_host = host.lower().strip()
        reasons_json = json.dumps(rejection_reasons or [])
        now = time.time()
        with self._lock:
            self.db.execute(
                """
                INSERT INTO host_concurrency_profiles (
                    host, verified_ceiling, denied_at_ceiling, cooldown_until,
                    last_probe_at, rejection_reasons_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host) DO UPDATE SET
                    verified_ceiling = excluded.verified_ceiling,
                    denied_at_ceiling = excluded.denied_at_ceiling,
                    cooldown_until = excluded.cooldown_until,
                    last_probe_at = excluded.last_probe_at,
                    rejection_reasons_json = excluded.rejection_reasons_json,
                    updated_at = excluded.updated_at
                """,
                (clean_host, verified_ceiling, denied_at_ceiling, cooldown_until, last_probe_at, reasons_json, now),
            )
            self.db.commit()

    def get_host_concurrency_profile(self, host: str) -> dict[str, Any] | None:
        clean_host = host.lower().strip()
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM host_concurrency_profiles WHERE host = ?",
                (clean_host,),
            ).fetchone()
            if not row:
                return None
            return {
                "host": row["host"],
                "verified_ceiling": row["verified_ceiling"],
                "denied_at_ceiling": row["denied_at_ceiling"],
                "cooldown_until": row["cooldown_until"],
                "last_probe_at": row["last_probe_at"],
                "rejection_reasons": json.loads(row["rejection_reasons_json"] or "[]"),
                "updated_at": row["updated_at"],
            }

    def list_host_concurrency_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM host_concurrency_profiles ORDER BY updated_at DESC"
            ).fetchall()
            return [
                {
                    "host": row["host"],
                    "verified_ceiling": row["verified_ceiling"],
                    "denied_at_ceiling": row["denied_at_ceiling"],
                    "cooldown_until": row["cooldown_until"],
                    "last_probe_at": row["last_probe_at"],
                    "rejection_reasons": json.loads(row["rejection_reasons_json"] or "[]"),
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

    def close(self) -> None:
        """Close SQLite database connection safely."""
        with self._lock:
            try:
                self.db.close()
            except Exception:
                pass
