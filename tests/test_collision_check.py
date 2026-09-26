from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.service import EngineService


class TestDestinationCollision(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.service = EngineService(str(self.data_dir))

    def tearDown(self):
        if hasattr(self.service, "store"):
            self.service.store.close()
        self.temp_dir.cleanup()

    def test_no_collision(self):
        result = self.service.dispatch("check_destination_collision", {
            "destination": str(self.data_dir),
            "filename": "new_file.mp4"
        })
        self.assertFalse(result["exists"])
        self.assertFalse(result["primary_exists"])
        self.assertIsNone(result["suggested_name"])

    def test_file_collision_suggests_copy(self):
        existing = self.data_dir / "movie.mp4"
        existing.write_bytes(b"content")

        result = self.service.dispatch("check_destination_collision", {
            "destination": str(self.data_dir),
            "filename": "movie.mp4"
        })
        self.assertTrue(result["exists"])
        self.assertTrue(result["primary_exists"])
        self.assertEqual(result["suggested_name"], "movie (1).mp4")
        self.assertEqual(result["size"], 7)

    def test_multiple_copies_increments_counter(self):
        (self.data_dir / "file.zip").write_bytes(b"1")
        (self.data_dir / "file (1).zip").write_bytes(b"2")
        (self.data_dir / "file (2).zip").write_bytes(b"3")

        result = self.service.dispatch("check_destination_collision", {
            "destination": str(self.data_dir),
            "filename": "file.zip"
        })
        self.assertTrue(result["exists"])
        self.assertEqual(result["suggested_name"], "file (3).zip")

    def test_item_collision_in_folder(self):
        (self.data_dir / "sub_item.srt").write_bytes(b"srt")
        result = self.service.dispatch("check_destination_collision", {
            "destination": str(self.data_dir),
            "filename": "nonexistent_folder",
            "items": ["sub_item.srt", "other.mp4"]
        })
        self.assertTrue(result["exists"])
        self.assertFalse(result["primary_exists"])
        self.assertIn("sub_item.srt", result["existing_items"])


if __name__ == "__main__":
    unittest.main()
