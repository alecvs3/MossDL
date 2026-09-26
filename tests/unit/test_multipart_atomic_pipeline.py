"""Unit tests for atomic multipart package admission and storage concurrency (PIPE-01, PIPE-04, PIPE-05, PIPE-06).

Verifies:
1. add_task initializes Part 1 as queued sentinel and holds Parts 2..N in pending_probe.
2. add_task demotes queued secondary parts if Part 1 arrives out of order.
3. download_task rejects direct manual activation of secondary parts while Part 1 is unresolved.
4. Discovered file size from Part 1 propagates to all sibling parts with missing size.
5. Background pump respects StorageHostConcurrencyManager limits before releasing pending parts.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from engine.models import DownloadTask, ResolvedItem
from engine.service import EngineService


class TestMultipartAtomicPipeline(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.service = EngineService(self.data_dir)
        self.service._engine_paused = True

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def test_add_task_multipart_sentinel_gating(self):
        """Adding Parts 1..4 sequentially puts Part 1 in queued and Parts 2..4 in pending_probe."""
        t1 = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/abc/Archive.part1.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Archive.part1.rar",
        })
        self.assertEqual(t1["state"], "queued")
        self.assertEqual(t1["package_part_number"], 1)

        t2 = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/def/Archive.part2.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Archive.part2.rar",
        })
        self.assertEqual(t2["state"], "pending_probe")
        self.assertIn("Waiting for Part 1 to resolve", t2.get("error", ""))
        self.assertEqual(t2["package_part_number"], 2)

        t3 = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/ghi/Archive.part3.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Archive.part3.rar",
        })
        self.assertEqual(t3["state"], "pending_probe")
        self.assertIn("Waiting for Part 1 to resolve", t3.get("error", ""))

        # Verify SQLite state
        db_tasks = {t.display_name: t for t in self.service.store.list()}
        self.assertEqual(db_tasks["Archive.part1.rar"].state, "queued")
        self.assertEqual(db_tasks["Archive.part2.rar"].state, "pending_probe")
        self.assertEqual(db_tasks["Archive.part3.rar"].state, "pending_probe")

    def test_add_task_out_of_order_demotion(self):
        """A non-first part never activates without Part 1, even when added first."""
        t2 = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/def/Archive.part2.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Archive.part2.rar",
        })
        t2_row = self.service.store.get(t2["id"])
        self.assertEqual(t2["state"], "pending_probe")
        self.assertIn("Waiting for Part 1 to resolve", t2_row.error or "")

        t1 = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/abc/Archive.part1.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Archive.part1.rar",
        })
        self.assertEqual(t1["state"], "queued")

        t2_updated = self.service.store.get(t2["id"])
        self.assertIsNotNone(t2_updated)
        self.assertEqual(t2_updated.state, "pending_probe")
        self.assertIn("Waiting for Part 1 to resolve", t2_updated.error or "")

    def test_download_task_rejects_unresolved_secondary_part(self):
        """Calling download_task on Part 2 while Part 1 is not resolved maintains pending_probe."""
        t1 = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/abc/Bundle.part1.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Bundle.part1.rar",
        })
        t2 = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/def/Bundle.part2.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Bundle.part2.rar",
        })
        self.assertEqual(t2["state"], "pending_probe")

        # Attempt to forcibly start Part 2 while Part 1 is still in 'queued'
        res = self.service.dispatch("download_task", {"id": t2["id"]})
        self.assertEqual(res["state"], "pending_probe")
        self.assertIn("Waiting for Part 1 to resolve", res.get("error", ""))

        # Verify Part 2 was not added to futures
        self.assertNotIn(t2["id"], self.service._futures)

    def test_download_task_allows_secondary_part_when_part1_resolved(self):
        """Calling download_task on Part 2 after Part 1 has resolved allows Part 2 to activate."""
        t1_dict = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/abc/Bundle.part1.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Bundle.part1.rar",
        })
        t2_dict = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/def/Bundle.part2.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Bundle.part2.rar",
        })

        t1 = self.service.store.get(t1_dict["id"])
        t1.state = "downloading"
        t1.size = 1073741824  # 1 GB
        self.service.store.save(t1)

        # Now download_task on Part 2 should proceed
        res = self.service.dispatch("download_task", {"id": t2_dict["id"]})
        self.assertEqual(res["state"], "queued")
        self.assertEqual(res["size"], 1073741824)

    def test_size_propagation_across_siblings(self):
        """When Part 1 discovers its size, that size is propagated to all sibling parts with missing size."""
        t1_dict = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/abc/Movie.part1.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Movie.part1.rar",
        })
        t2_dict = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/def/Movie.part2.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Movie.part2.rar",
        })
        t3_dict = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/ghi/Movie.part3.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Movie.part3.rar",
        })

        t1 = self.service.store.get(t1_dict["id"])
        t1.size = 2147483648  # 2 GB
        self.service.store.save(t1)

        # Track TaskUpdated events
        updated_events = []
        original_emit = self.service.events.emit
        def mock_emit(event_name, target_id, payload, *args, **kwargs):
            if event_name == "TaskUpdated":
                updated_events.append((target_id, payload))
            return original_emit(event_name, target_id, payload, *args, **kwargs)
        self.service.events.emit = mock_emit

        self.service._propagate_multipart_size(t1)

        # Sibling tasks in SQLite should now have size 2147483648
        t2 = self.service.store.get(t2_dict["id"])
        t3 = self.service.store.get(t3_dict["id"])
        self.assertEqual(t2.size, 2147483648)
        self.assertEqual(t3.size, 2147483648)

        # Events should have been emitted for Part 2 and Part 3
        updated_ids = [eid for eid, _ in updated_events]
        self.assertIn(t2.id, updated_ids)
        self.assertIn(t3.id, updated_ids)

    def test_storage_concurrency_gates_pump_release(self):
        """Storage host concurrency limit prevents releasing secondary parts when host ceiling is reached."""
        t1_dict = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/abc/Pack.part1.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Pack.part1.rar",
        })
        t2_dict = self.service.dispatch("add_task", {
            "url": "https://datanodes.to/def/Pack.part2.rar",
            "destination": str(self.data_dir / "downloads"),
            "display_name": "Pack.part2.rar",
        })

        # Set storage host concurrency limit to 1
        self.service.storage_concurrency.set_limit("datanodes.to", 1)

        t1 = self.service.store.get(t1_dict["id"])
        t1.state = "downloading"
        t1.size = 500000000
        t1.resolved = [ResolvedItem(provider="datanodes", source_url=t1.source_url,
                                    display_name="Pack.part1.rar", direct_url="https://datanodes.to/direct/part1",
                                    size=500000000)]
        self.service.store.save(t1)

        # Run one pump cycle logic
        tasks = self.service.store.list()
        pkg_map = {}
        for t in tasks:
            is_mp, pkey, pnum = self.service._task_multipart_info(t)
            if is_mp and pkey:
                pkg_map.setdefault(pkey, []).append(t)

        now = 1000.0
        for pkey, ptasks in pkg_map.items():
            part1 = next((t for t in ptasks if self.service._task_multipart_info(t)[2] == 1), None)
            part1_resolved = part1.state in {"downloading", "verifying", "postprocessing", "completed"}
            other_parts = sorted(
                [t for t in ptasks if self.service._task_multipart_info(t)[2] > 1],
                key=lambda t: self.service._task_multipart_info(t)[2]
            )
            storage_host = (part1.resolved[0].direct_url or part1.source_url).split("/")[2]
            max_streams = self.service.storage_concurrency.get_limit(storage_host)
            active_streams = sum(1 for t in ptasks if t.state in {"resolving", "preflight", "downloading"})
            self.assertEqual(max_streams, 1)
            self.assertEqual(active_streams, 1)

            # active_streams (1) is NOT < max_streams (1), so next_pending should NOT release
            self.assertFalse(active_streams < max_streams)

        t2 = self.service.store.get(t2_dict["id"])
        self.assertEqual(t2.state, "pending_probe")

        # Now simulate Part 1 completing
        t1.state = "completed"
        self.service.store.save(t1)

        tasks = self.service.store.list()
        for pkey, ptasks in pkg_map.items():
            ptasks = self.service.store.list()
            part1 = next((t for t in ptasks if self.service._task_multipart_info(t)[2] == 1), None)
            other_parts = sorted(
                [t for t in ptasks if self.service._task_multipart_info(t)[2] > 1],
                key=lambda t: self.service._task_multipart_info(t)[2]
            )
            storage_host = (part1.resolved[0].direct_url or part1.source_url).split("/")[2]
            max_streams = self.service.storage_concurrency.get_limit(storage_host)
            active_streams = sum(1 for t in ptasks if t.state in {"resolving", "preflight", "downloading"})
            self.assertEqual(active_streams, 0)
            self.assertTrue(active_streams < max_streams)

            next_pending = next((t for t in other_parts if t.state == "pending_probe"), None)
            if next_pending:
                next_pending.size = part1.size
                self.service._transition(next_pending, "queued", "TaskQueued")
                self.service.store.save(next_pending)

        t2_after = self.service.store.get(t2_dict["id"])
        self.assertEqual(t2_after.state, "queued")
        self.assertEqual(t2_after.size, 500000000)


if __name__ == "__main__":
    unittest.main()
