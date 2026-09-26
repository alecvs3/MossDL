from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import shutil
import unittest
from pathlib import Path

from engine.db import TaskStore
from engine.file_classifier import classify_file
from engine.models import DownloadTask
from engine.service import EngineService


class IntakeControlTests(unittest.TestCase):
    def test_classifier_precedence_and_case_insensitive_extensions(self):
        self.assertEqual(classify_file(mime="video/mp4", filename="cover.jpg").category, "video")
        self.assertEqual(classify_file(filename="TRACK.FLAC").category, "music")
        self.assertEqual(classify_file(source_url="https://cdn.test/report.PDF").category, "documents")
        self.assertEqual(classify_file(filename="unknown.bin").category, "other")

    def test_classifier_covers_every_common_download_kind(self):
        expected = {
            "clip.ts": "video", "movie.m2ts": "video", "book.cbz": "ebooks", "disc.iso": "disk_images",
            "ui.woff2": "fonts", "mascot.glb": "models_3d", "pack.torrent": "torrents", "check.sfv": "checksums",
            "pack.part2.rar": "archives", "pack.r00": "archives", "pack.7z.001": "archives", "photo.CR3": "pictures",
        }
        for name, category in expected.items():
            self.assertEqual(classify_file(filename=name).category, category, name)
        self.assertEqual(classify_file(mime="font/woff2").category, "fonts")

    def test_classifier_overrides_apply_to_extension_stages(self):
        result = classify_file(filename="payload.bin", overrides={".bin": "archives"})
        self.assertEqual(result.category, "archives")
        self.assertEqual(result.source, "resolved_extension")

    def test_task_telemetry_round_trip_is_additive(self):
        root = Path(__file__).resolve().parent / ".test-artifacts" / "intake-controls"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        store = TaskStore(root / "tasks.sqlite3")
        try:
            task = DownloadTask("https://example.test/file.mp4", str(root), category="video",
                                folder_path=str(root), speed_bytes_per_second=1234.5,
                                average_speed_bytes_per_second=1000.0, eta_seconds=12.0,
                                backend="custom", attempt_count=2, integrity_state="verified")
            store.save(task)
            loaded = store.get(task.id)
            self.assertEqual(loaded.category, "video")
            self.assertEqual(loaded.backend, "custom")
            self.assertEqual(loaded.integrity_state, "verified")
            self.assertAlmostEqual(loaded.speed_bytes_per_second, 1234.5)
        finally:
            store.close()
            shutil.rmtree(root, ignore_errors=True)

    def test_global_pause_and_stop_preserve_task_history(self):
        root = Path(__file__).resolve().parent / ".test-artifacts" / "intake-controls-service"
        shutil.rmtree(root, ignore_errors=True)
        service = EngineService(root)
        try:
            task = service.dispatch("add_task", {"url": "https://example.test/file.bin", "destination": str(root)})
            paused = service.dispatch("tasks_pause_all", {})
            self.assertIn(task["id"], paused["affected_ids"])
            self.assertEqual(service.store.get(task["id"]).state, "paused")
            stopped = service.dispatch("tasks_stop_all", {})
            self.assertIn(task["id"], stopped["affected_ids"])
            self.assertEqual(service.store.get(task["id"]).state, "canceled")
        finally:
            service.close()
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
