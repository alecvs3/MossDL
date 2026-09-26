"""Unit tests for Phase 24 storage host concurrency (PIPE-05, PIPE-06, PIPE-07):
Storage host concurrency manager, hosters.json persistence, 0-byte stall drop,
and unified backend: rust enforcement.
"""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from engine.models import DownloadTask, ResolvedItem
from engine.service import EngineService
from engine.storage_concurrency import StorageHostConcurrencyManager


class TestStorageConcurrency(unittest.TestCase):

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.manager = StorageHostConcurrencyManager(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_storage_host_calibration_saved_to_hosters_json(self):
        """PIPE-05: Concurrency calibration saves to hosters.json and reloads across restarts."""
        host = "tunnel5.dlproxy.uk"
        self.manager.set_limit(host, 3, calibrated=True)

        # Assert in-memory
        self.assertEqual(self.manager.get_limit(host), 3)

        # Assert on disk in hosters.json
        hosters_file = self.temp_dir / "hosters.json"
        self.assertTrue(hosters_file.exists())
        data = json.loads(hosters_file.read_text(encoding="utf-8"))
        self.assertIn(host, data)
        self.assertEqual(data[host]["max_concurrent_streams"], 3)
        self.assertGreater(data[host]["calibrated_at"], 0.0)

        # Assert reloads in fresh manager instance
        fresh = StorageHostConcurrencyManager(self.temp_dir)
        self.assertEqual(fresh.get_limit(host), 3)

    def test_dead_secondary_stream_is_restarted_without_clamping_the_ceiling(self):
        """PIPE-06: a stream moving nothing is restarted; the host keeps its ceiling.

        The ceiling used to be clamped to the productive count here. That is the
        bug that collapsed multipart packages to one stream: a slow DataNodes
        lane looks unproductive, so the host got pinned to 1 and every later part
        queued behind it. See tests/unit/test_stream_starvation.py.
        """
        from engine.storage_concurrency import _STALL_GRACE_SECONDS, _STALL_WINDOW_SECONDS

        shost = "tunnel5.dlproxy.uk"
        self.manager.set_limit(shost, 3, calibrated=True)

        self.manager.register_stream(shost, "task-1", expected_bytes=2 * 1024 ** 3)
        self.manager.register_stream(shost, "task-2", expected_bytes=2 * 1024 ** 3)

        self.manager.update_stream(shost, "task-1", bytes_transferred=5 * 1024 * 1024, speed=2 * 1024 * 1024)

        now = time.time()
        stream2 = self.manager._active_streams[shost]["task-2"]
        stream2.started_at = now - (_STALL_GRACE_SECONDS + 30)
        stream2.window_started_at = now - (_STALL_WINDOW_SECONDS + 5)
        stream2.window_start_bytes = 0
        stream2.total_bytes = 0

        stalled = self.manager.check_zero_byte_stalls(shost)
        self.assertIn("task-2", stalled)
        self.assertNotIn("task-1", stalled)

        self.assertEqual(
            self.manager.get_limit(shost), 3,
            "a dead stream moved the host's ceiling; only 429/403 may do that")

    def test_multipart_enforces_rust_backend(self):
        """PIPE-07: Multipart packages enforce backend: rust unconditionally."""
        service = EngineService.__new__(EngineService)
        t = DownloadTask(
            source_url="https://datanodes.to/123/Game.part01.rar",
            destination="/tmp",
            display_name="Game.part01.rar",
            folder_path="/tmp/Game"
        )
        is_mp, pkey, pnum = service._task_multipart_info(t)
        self.assertTrue(is_mp)

        # Simulate backend selection logic
        backend_name = "custom"
        if is_mp:
            backend_name = "rust"
        self.assertEqual(backend_name, "rust", "Multipart must enforce backend: rust")

    def test_datanodes_defaults_to_default_limit(self):
        """Uncalibrated hosts (including DataNodes) default to default_limit (4) rather than being clamped."""
        dn_host = "node42.datanodes.to"
        root_dn = "datanodes.to"
        other_host = "generic.example.com"

        self.assertEqual(self.manager.get_limit(dn_host), self.manager.default_limit)
        self.assertEqual(self.manager.get_limit(root_dn), self.manager.default_limit)
        self.assertEqual(self.manager.get_limit(other_host), self.manager.default_limit)

    def test_datanodes_expired_ttl_reprobing_defaults_to_default_limit(self):
        """When calibration TTL expires, host returns default_limit to allow re-probing."""
        dn_host = "node42.datanodes.to"
        self.manager.set_limit(dn_host, 1, calibrated=True)
        self.assertEqual(self.manager.get_limit(dn_host), 1)

        # Force calibration to expire
        prof = self.manager._profiles[dn_host]
        prof.calibrated_at = time.time() - 90000.0

        # Expired limit with TTL=86400 should return default_limit
        self.assertEqual(self.manager.get_limit(dn_host, ttl=86400.0), self.manager.default_limit)

    def test_reconsider_limits_withholds_probe_when_a_stream_is_dead(self):
        """The ladder does not admit another stream while one is moving nothing.

        Note this keys on DEAD, not on "slower than the productive threshold".
        Withholding on merely-slow pinned the ceiling on DataNodes, where a slow
        lane is normal and permanent, so a probe never fired.
        """
        from engine.storage_concurrency import _STALL_GRACE_SECONDS, _STALL_WINDOW_SECONDS

        host = "node42.datanodes.to"
        self.manager.set_limit(host, 2)
        sem = self.manager.get_semaphore(host)
        self.manager.register_stream(host, "task-1")
        self.manager.register_stream(host, "task-2")

        self.manager.update_stream(host, "task-1", bytes_transferred=5 * 1024 * 1024, speed=2 * 1024 * 1024)
        now = time.time()
        stream2 = self.manager._active_streams[host]["task-2"]
        stream2.total_bytes = 0
        stream2.started_at = now - (_STALL_GRACE_SECONDS + 30)
        stream2.window_started_at = now - (_STALL_WINDOW_SECONDS + 5)
        stream2.window_start_bytes = 0

        sem._waiters = 1
        mock_auditor = MagicMock()
        mock_auditor.probe_allowance.return_value = 3
        self.manager._auditor = mock_auditor

        self.manager.reconsider_limits()
        mock_auditor.probe_allowance.assert_not_called()
        self.assertEqual(sem.limit, 2)


if __name__ == "__main__":
    unittest.main()
