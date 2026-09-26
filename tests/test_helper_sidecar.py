from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

from engine.helper_sidecar import HelperSupervisor
from engine.limits import ResourceManager, SchedulerPolicy
from engine.secrets import InMemorySecretBackend, SecretManager


class Events:
    def __init__(self):
        self.items = []

    def emit(self, name, task_id, payload, dedupe_key=None):
        self.items.append({"event_type": name, "task_id": task_id, "payload": payload, "dedupe_key": dedupe_key})


class HelperSidecarTests(unittest.TestCase):
    def fixture(self, body: str):
        root = Path(tempfile.mkdtemp(prefix="helper-fixture-"))
        path = root / "helper.py"
        path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return root, path, {"id": "yt-dlp", "version": "2026.01", "executable": str(path),
                            "sha256": digest, "operations": ["media", "decrypt"],
                            "hosts": ["video.example"]}

    def test_real_worker_path_reports_progress_and_redacted_result(self):
        _, _, entry = self.fixture("""
            import json, os, sys
            params = json.loads(sys.stdin.readline())
            print(json.dumps({'type':'progress','done':1,'total':2}), flush=True)
            print(json.dumps({'type':'progress','done':2,'total':2}), flush=True)
            print(json.dumps({'type':'result','status':'completed','value':{'url':params['url'], 'cookie':os.environ.get('HELPER_CREDENTIAL')}}), flush=True)
        """)
        events = Events()
        secrets = SecretManager(InMemorySecretBackend())
        ref = secrets.put("cookie=raw-cookie", kind="cookie")
        outcome = HelperSupervisor([entry], secret_manager=secrets, event_publisher=events).run(
            "yt-dlp", "media", "https://video.example/watch", {"url": "https://video.example/watch?token=raw-token"},
            credential_ref=ref, task_id="task-1")
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(len(outcome.progress), 2)
        serialized = json.dumps(outcome.to_dict()) + json.dumps(events.items)
        self.assertNotIn("raw-cookie", serialized)
        self.assertNotIn("raw-token", serialized)
        self.assertTrue(any(item["event_type"] == "HelperProgress" for item in events.items))

    def test_hash_and_allowlist_fail_closed_before_execution(self):
        root, path, entry = self.fixture("print('should not run')")
        entry["sha256"] = "0" * 64
        outcome = HelperSupervisor([entry]).run("yt-dlp", "media", "https://video.example/watch")
        self.assertEqual(outcome.status, "hash_mismatch")
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(HelperSupervisor([entry]).run("yt-dlp", "unknown", "https://video.example/watch").status, "unsupported")
        self.assertEqual(HelperSupervisor([entry]).run("yt-dlp", "media", "https://other.example/watch").status, "unsupported")

    def test_cancellation_timeout_quota_and_crash_release_permits(self):
        _, _, entry = self.fixture("""
            import json, time
            params = json.loads(__import__('sys').stdin.readline())
            if params.get('mode') == 'sleep':
                time.sleep(10)
            elif params.get('mode') == 'crash':
                raise SystemExit(3)
            print(json.dumps({'type':'result','status':'completed','value':{}}), flush=True)
        """)
        supervisor = HelperSupervisor([entry], max_active=1, worker_timeout=0.2)
        self.assertEqual(supervisor.run("yt-dlp", "media", "https://video.example", {"mode": "sleep"}).status, "timed_out")
        self.assertEqual(supervisor._active, 0)
        cancel = threading.Event(); cancel.set()
        self.assertEqual(supervisor.run("yt-dlp", "media", "https://video.example", {"mode": "sleep"}, cancel_event=cancel).status, "cancelled")
        crash_supervisor = HelperSupervisor([entry], worker_timeout=2.0)
        self.assertEqual(crash_supervisor.run("yt-dlp", "media", "https://video.example", {"mode": "crash"}).status, "crashed")

    def test_quota_is_bounded_without_parallel_queue(self):
        _, _, entry = self.fixture("""
            import json, time
            json.loads(__import__('sys').stdin.readline()); time.sleep(0.2)
            print(json.dumps({'type':'result','status':'completed','value':{}}), flush=True)
        """)
        supervisor = HelperSupervisor([entry], max_active=0)
        self.assertEqual(supervisor.run("yt-dlp", "media", "https://video.example").status, "quota_exceeded")

    def test_malformed_progress_and_output_limits_are_classified(self):
        _, _, malformed = self.fixture("""
            import json
            json.loads(__import__('sys').stdin.readline())
            print(json.dumps({'type':'progress','done':'one','total':2}), flush=True)
        """)
        self.assertEqual(HelperSupervisor([malformed]).run("yt-dlp", "media", "https://video.example").status,
                         "malformed_progress")
        _, _, oversized = self.fixture("""
            import json
            json.loads(__import__('sys').stdin.readline())
            print(json.dumps({'type':'result','status':'completed','value':{'blob':'x' * 3000}}), flush=True)
        """)
        self.assertEqual(HelperSupervisor([oversized], max_output_bytes=1024).run(
            "yt-dlp", "media", "https://video.example").status, "output_limit")

    def test_child_environment_is_minimal_and_credential_is_operation_scoped(self):
        _, _, entry = self.fixture("""
            import json, os
            json.loads(__import__('sys').stdin.readline())
            allowed = sorted(key for key in os.environ if key.startswith('HELPER_') or key in {'PATH','PYTHONUNBUFFERED'})
            print(json.dumps({'type':'result','status':'completed','value':{'env':allowed, 'credential':os.environ.get('HELPER_CREDENTIAL')}}), flush=True)
        """)
        secrets = SecretManager(InMemorySecretBackend())
        ref = secrets.put("operation-secret", kind="token")
        outcome = HelperSupervisor([entry], secret_manager=secrets).run(
            "yt-dlp", "media", "https://video.example", credential_ref=ref)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.result["env"], ["HELPER_CREDENTIAL", "PATH", "PYTHONUNBUFFERED"])
        self.assertEqual(outcome.result["credential"], "[redacted]")

    def test_resource_manager_execution_has_async_admission_path(self):
        _, _, entry = self.fixture("""
            import json
            json.loads(__import__('sys').stdin.readline())
            print(json.dumps({'type':'result','status':'completed','value':{}}), flush=True)
        """)
        resources = ResourceManager(SchedulerPolicy(max_active_tasks=1))
        outcome = asyncio.run(HelperSupervisor([entry], resource_manager=resources).run_async(
            "yt-dlp", "media", "https://video.example"))
        self.assertEqual(outcome.status, "completed")

    def test_engine_authority_params_are_rejected_before_launch(self):
        _, _, entry = self.fixture("raise SystemExit('must not run')")
        outcome = HelperSupervisor([entry]).run("yt-dlp", "media", "https://video.example",
                                               {"scheduler": "take-over"})
        self.assertEqual(outcome.status, "unsupported")


if __name__ == "__main__":
    unittest.main()
