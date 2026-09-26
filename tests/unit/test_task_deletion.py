import tempfile
import unittest
from pathlib import Path


from engine.models import DownloadTask
from engine.service import EngineService


class TestTaskDeletion(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.service = EngineService(self.data_dir)


    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()


    def test_delete_task_prevents_resurrection(self):
        task = DownloadTask(
            source_url="https://example.com/testfile.bin",
            destination=str(self.data_dir / "testfile.bin"),
            state="downloading",
        )
        self.service.store.save(task)
        self.assertIsNotNone(self.service.store.get(task.id))

        result = self.service.dispatch("delete_task", {"id": task.id})
        self.assertEqual(result.get("deleted"), task.id)
        self.assertIsNone(self.service.store.get(task.id))
        self.assertIn(task.id, self.service._deleted_task_ids)


        task.state = "canceled"
        self.service.store.save(task)
        self.assertIsNone(self.service.store.get(task.id))

        self.service._transition(task, "canceled", "TaskCanceled", "User canceled")
        self.assertIsNone(self.service.store.get(task.id))

    def test_delete_task_removes_files_from_disk(self):
        downloads_dir = self.data_dir / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)


        target_file = downloads_dir / "movie.mp4"
        target_file.write_bytes(b"some content")
        part_file = downloads_dir / "movie.mp4.part"
        part_file.write_bytes(b"partial content")
        crdownload_file = downloads_dir / "movie.mp4.crdownload"
        crdownload_file.write_bytes(b"crdownload content")

        task = DownloadTask(
            source_url="https://example.com/movie.mp4",
            destination=str(target_file),
            display_name="movie.mp4",
            state="downloading",
        )
        self.service.store.save(task)

        self.service.dispatch("delete_task", {"id": task.id, "delete_files": False})
        self.assertTrue(target_file.exists())
        self.assertTrue(part_file.exists())

        task2 = DownloadTask(
            source_url="https://example.com/movie.mp4",
            destination=str(target_file),
            display_name="movie.mp4",
            state="downloading",
        )
        self.service.store.save(task2)

        res = self.service.dispatch("delete_task", {"id": task2.id, "delete_files": True})
        self.assertEqual(res.get("deleted"), task2.id)
        self.assertFalse(target_file.exists())
        self.assertFalse(part_file.exists())
        self.assertFalse(crdownload_file.exists())


    def test_batch_delete_tasks_with_files(self):
        downloads_dir = self.data_dir / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)

        file1 = downloads_dir / "file1.bin"
        file1.write_bytes(b"data1")
        file1_part = downloads_dir / "file1.bin.part"
        file1_part.write_bytes(b"part1")

        file2 = downloads_dir / "file2.bin"
        file2.write_bytes(b"data2")

        task1 = DownloadTask(
            source_url="https://example.com/file1.bin",
            destination=str(file1),
            display_name="file1.bin",
            state="completed",
        )
        task2 = DownloadTask(
            source_url="https://example.com/file2.bin",
            destination=str(file2),
            display_name="file2.bin",
            state="completed",
        )
        self.service.store.save(task1)
        self.service.store.save(task2)

        result = self.service.dispatch("delete_task", {
            "ids": [task1.id, task2.id],
            "delete_files": True,
        })
        self.assertEqual(len(result.get("affected_ids", [])), 2)
        self.assertIsNone(self.service.store.get(task1.id))
        self.assertIsNone(self.service.store.get(task2.id))
        self.assertFalse(file1.exists())
        self.assertFalse(file1_part.exists())
        self.assertFalse(file2.exists())


if __name__ == "__main__":
    unittest.main()
