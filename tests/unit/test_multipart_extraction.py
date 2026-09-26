import tempfile
import unittest
from pathlib import Path
from engine.archive_joiner import MultiPartDetector, BinaryPartJoiner, ArchiveOrchestrator
from engine.archive_backend import ArchiveBackend

class TestMultipartExtraction(unittest.TestCase):
    def test_detector_patterns(self):
        # 1. Standard .partX.ext
        res1 = MultiPartDetector.detect("CoolGame.part01.rar")
        self.assertIsNotNone(res1)
        self.assertEqual(res1.base_name, "CoolGame")
        self.assertEqual(res1.part_number, 1)
        self.assertEqual(res1.format_type, "part_archive")
        self.assertEqual(res1.extension, "rar")

        # 2. Split binary
        res2 = MultiPartDetector.detect("archive.iso.003")
        self.assertIsNotNone(res2)
        self.assertEqual(res2.base_name, "archive.iso")
        self.assertEqual(res2.part_number, 3)
        self.assertEqual(res2.format_type, "split_binary")

        # 3. Legacy RAR
        res3 = MultiPartDetector.detect("backup.r02")
        self.assertIsNotNone(res3)
        self.assertEqual(res3.base_name, "backup")
        self.assertEqual(res3.part_number, 4)
        self.assertEqual(res3.format_type, "numbered_rar")

    def test_split_binary_join(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            p1 = tmp / "data.001"
            p2 = tmp / "data.002"
            p1.write_bytes(b"HELLO ")
            p2.write_bytes(b"WORLD!")

            out = tmp / "joined.txt"
            joined_path, sha, size = BinaryPartJoiner.join([p1, p2], out)
            self.assertTrue(out.is_file())
            self.assertEqual(out.read_bytes(), b"HELLO WORLD!")
            self.assertEqual(size, 12)

    def test_archive_worker_available(self):
        backend = ArchiveBackend()
        self.assertTrue(backend.available(), "archive-worker binary must be available in target/debug or resources")

    def test_batch_multipart_detection(self):
        # Test detection across multiple filenames in a batch
        batch = [
            "https://rapidgator.net/file/1/Game.part01.rar",
            "https://rapidgator.net/file/2/Game.part02.rar",
            "https://rapidgator.net/file/3/Game.part03.rar",
        ]
        results = [MultiPartDetector.detect(u) for u in batch]
        self.assertTrue(all(r is not None and r.is_multipart for r in results))
        self.assertEqual({r.base_name for r in results if r}, {"Game"})
        self.assertEqual([r.part_number for r in results if r], [1, 2, 3])

if __name__ == "__main__":
    unittest.main()
