"""What survives a relaunch: downloads pause with their progress, History stays,
and Explore starts empty."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.db import TaskStore  # noqa: E402
from engine.models import DownloadTask  # noqa: E402
from engine.service import EngineService  # noqa: E402


class RelaunchTests(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.data = Path(self._dir.name)

    def test_unfinished_downloads_come_back_paused_with_their_progress(self):
        # The database as a closed app left it: no engine running while it is written.
        store = TaskStore(self.data / "downloads.sqlite3")
        ids = []
        for state in ("downloading", "queued", "resolving"):
            task = DownloadTask(f"https://example.test/{state}.bin", str(self.data / "dl"), id=f"task-{state}")
            task.state, task.completed_bytes = state, 5_000
            store.save(task)
            ids.append(task.id)
        store.close()
        relaunched = EngineService(self.data)
        try:
            for task_id in ids:
                task = relaunched.store.get(task_id)
                self.assertEqual(task.state, "paused", "nothing starts by itself after a relaunch")
                self.assertEqual(task.completed_bytes, 5_000, "progress is kept")
                self.assertIn("Resume", task.paused_reason)
        finally:
            relaunched.close()

    def test_history_outlives_removing_the_download_and_has_its_own_clear(self):
        service = EngineService(self.data)
        try:
            stored = DownloadTask("https://example.test/done.bin", str(self.data / "dl"), id="task-done")
            stored.state = "downloading"
            service.store.save(stored)
            service._transition(stored, "completed", "TaskCompleted")
            service.dispatch("delete_task", {"id": stored.id})
            history = service.dispatch("history_list", {})
            self.assertEqual([h["id"] for h in history], [stored.id])
            self.assertEqual(history[0]["state"], "completed")
            self.assertEqual(service.dispatch("history_clear", {})["removed"], 1)
            self.assertEqual(service.dispatch("history_list", {}), [])
        finally:
            service.close()

    def test_explore_starts_empty_after_a_launch(self):
        service = EngineService(self.data)
        try:
            service.dispatch("linkgrabber_add", {"text": "https://a.test/x.zip https://b.test/y.zip"})
            self.assertTrue(service.store.list_links())
            service._wipe_explore_on_launch()
            self.assertEqual(service.store.list_links(), [])
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
