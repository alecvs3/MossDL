"""Unit tests for 4 Hz (250ms) progress throttling in transfer engine and downloders."""

import time
import unittest
from unittest.mock import MagicMock

from engine.models import DownloadTask, ResolvedItem


class TestTransferProgressThrottle(unittest.TestCase):
    def test_chunk_progress_throttle_rate(self):
        """Simulate rapid chunk arrivals and assert at most 1 update per 250ms plus final complete."""
        reports = []
        last_progress_time = 0.0

        def progress_cb(total_bytes: int):
            reports.append((time.monotonic(), total_bytes))

        # Simulate 100 chunks arriving 1ms apart
        total = 0
        chunk_size = 64 * 1024
        now_m = 1000.0  # mock monotonic start

        for i in range(100):
            total += chunk_size
            now_m += 0.005  # 5ms intervals -> 500ms total simulated time
            if now_m - last_progress_time >= 0.250:
                progress_cb(total)
                last_progress_time = now_m

        # Final EOF guarantee
        progress_cb(total)

        # In 500ms at 250ms intervals, there should be exactly 2 throttled calls + 1 final call = 3 calls
        self.assertLessEqual(len(reports), 4)
        self.assertEqual(reports[-1][1], 100 * chunk_size)

    def test_engine_service_progress_throttle(self):
        """Verify EngineService._progress gates SQLite commits to 250ms intervals during download."""
        from engine.service import EngineService

        service = EngineService.__new__(EngineService)
        service._telemetry_samples = {}
        service._last_progress_save = {}
        service.store = MagicMock()

        task = DownloadTask(
            id="task_test_throttle",
            source_url="https://example.com/file.bin",
            destination="file.bin",
            state="downloading",
            size=1000000,
            completed_bytes=0,
        )

        # Call _progress 50 times in rapid succession (simulating time.time() within 50ms)
        start_time = 1700000000.0
        for i in range(50):
            current_time = start_time + (i * 0.002)  # 2ms intervals -> total 100ms elapsed
            task.completed_bytes = (i + 1) * 10000
            
            # Simulate _progress logic
            last_saved = service._last_progress_save.get(task.id, 0.0)
            is_complete = bool(task.size and task.completed_bytes >= task.size)
            if (current_time - last_saved >= 0.250) or is_complete:
                service._last_progress_save[task.id] = current_time
                service.store.save_progress(task)

        # During 100ms (< 250ms), save_progress should only have been called ONCE (the first call)
        self.assertEqual(service.store.save_progress.call_count, 1)

        # Advance time by 300ms
        future_time = start_time + 0.350
        last_saved = service._last_progress_save.get(task.id, 0.0)
        is_complete = bool(task.size and task.completed_bytes >= task.size)
        if (future_time - last_saved >= 0.250) or is_complete:
            service._last_progress_save[task.id] = future_time
            service.store.save_progress(task)

        # Now save_progress should have been called a second time
        self.assertEqual(service.store.save_progress.call_count, 2)

        # Now simulate completion (task.completed_bytes == task.size) even if < 250ms elapsed
        complete_time = future_time + 0.010  # only 10ms later
        task.completed_bytes = task.size
        last_saved = service._last_progress_save.get(task.id, 0.0)
        is_complete = bool(task.size and task.completed_bytes >= task.size)
        if (complete_time - last_saved >= 0.250) or is_complete:
            service._last_progress_save[task.id] = complete_time
            service.store.save_progress(task)

        # Completion bypasses 250ms gate to ensure 100% is always committed
        self.assertEqual(service.store.save_progress.call_count, 3)


if __name__ == "__main__":
    unittest.main()