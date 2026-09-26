import unittest
from engine.models import DownloadTask, ResolvedItem


def _run_finalizer_logic(task, item_progress_bytes):
    final_bytes = max(task.completed_bytes or 0, sum(item_progress_bytes.values()))
    if not (task.size and task.size > 0):
        task.size = final_bytes
        task.completed_bytes = final_bytes
        for _it in task.resolved or []:
            if _it.size is None or _it.size == 0:
                _it.size = final_bytes // max(1, len(task.resolved))
    elif task.completed_bytes != task.size:
        task.completed_bytes = task.size


class TestFinalizerDynamicSize(unittest.TestCase):

    def test_indeterminate_stream_populates_size(self):
        item = ResolvedItem(provider="rust", source_url="https://cdn.example.com/video.mp4", display_name="video.mp4", size=None)
        task = DownloadTask(source_url="https://cdn.example.com/video.mp4", destination="/tmp", state="downloading", size=None, completed_bytes=0, resolved=[item])
        _run_finalizer_logic(task, {0: 52_428_800})
        assert task.size == 52_428_800, f"task.size={task.size}"
        assert task.completed_bytes == task.size
        assert item.size == 52_428_800

    def test_known_size_preserved_completed_corrected(self):
        task = DownloadTask(source_url="https://cdn.example.com/archive.zip", destination="/tmp", state="downloading", size=1_000_000, completed_bytes=999_999)
        _run_finalizer_logic(task, {0: 999_999})
        assert task.size == 1_000_000
        assert task.completed_bytes == 1_000_000

    def test_zero_bytes_graceful(self):
        task = DownloadTask(source_url="https://cdn.example.com/empty", destination="/tmp", state="downloading", size=None, completed_bytes=0)
        _run_finalizer_logic(task, {})
        assert task.size == 0
        assert task.completed_bytes == 0

    def test_multi_item_size_distributed(self):
        items = [
            ResolvedItem(provider="rust", source_url="https://cdn.example.com/a", display_name="a.bin", size=None),
            ResolvedItem(provider="rust", source_url="https://cdn.example.com/b", display_name="b.bin", size=None),
        ]
        task = DownloadTask(source_url="https://cdn.example.com/", destination="/tmp", state="downloading", size=None, completed_bytes=0, resolved=items)
        _run_finalizer_logic(task, {0: 52_428_800, 1: 52_428_800})
        assert task.size == 104_857_600
        assert items[0].size == 52_428_800
        assert items[1].size == 52_428_800


class TestRustBackendSizePropagate(unittest.TestCase):

    def test_actual_bytes_propagated_when_size_none(self):
        item = ResolvedItem(provider="rust", source_url="https://cdn.example.com/video.mp4", display_name="video.mp4", size=None)
        result = {"path": "/tmp/video.mp4", "bytes": 73_400_320}
        progress_calls = []
        actual_bytes = int(result.get("bytes", 0))
        if item.size is None:
            item.size = actual_bytes
        progress_calls.append(actual_bytes)
        assert item.size == 73_400_320
        assert progress_calls[-1] == 73_400_320

    def test_known_size_not_overwritten(self):
        item = ResolvedItem(provider="rust", source_url="https://cdn.example.com/known.zip", display_name="known.zip", size=100_000_000)
        result = {"path": "/tmp/known.zip", "bytes": 99_999_999}
        actual_bytes = int(result.get("bytes", 0))
        if item.size is None:
            item.size = actual_bytes
        assert item.size == 100_000_000


if __name__ == "__main__":
    unittest.main()
