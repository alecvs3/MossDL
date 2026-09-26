from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from engine.models import DownloadTask, ResolvedItem
from engine.service import EngineService
from engine.archive_backend import completion_sentinel_path
from engine.archive_joiner import MultiPartDetector


class _MockArchiveWorkerBackend:
    """Simulates the isolated Rust archive worker producing realistic extracted files."""

    def __init__(self, output_files: dict[str, bytes] | None = None) -> None:
        self.output_files = output_files or {
            "RuneScape_Dragonwilds/game.exe": b"MZ\x90\x00gameexecutable",
            "RuneScape_Dragonwilds/data.pak": b"PACKDATA" * 512,
        }
        self.calls = 0

    def available(self) -> bool:
        return True

    def extract(
        self,
        path: str | Path,
        output_directory: str | Path,
        password_ref: str | None = None,
        policy: dict[str, Any] | None = None,
        control: Any = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        out = Path(output_directory)
        total_bytes = 0
        file_count = 0
        for rel_path, data in self.output_files.items():
            target = out / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            total_bytes += len(data)
            file_count += 1
        if job_id:
            completion_sentinel_path(out, job_id).write_bytes(b"")
        return {"output_directory": str(out), "files": file_count, "bytes": total_bytes}


class TestMultipartLifecycleE2E(unittest.TestCase):
    """Full lifecycle integration tests for multi-part archive handling."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.downloads_dir = self.data_dir / "downloads"
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.service = EngineService(self.data_dir)
        self.service._engine_paused = True

    def tearDown(self) -> None:
        self.service.close()
        self.temp_dir.cleanup()

    def test_e2e_intake_sentinel_gating_and_package_key_stability(self) -> None:
        """Intake of realistic multi-part URLs assigns uniform package key and sentinel gating."""
        urls = [
            "https://datanodes.to/3rycs4yr8ukd/RuneScape_Dragonwilds_--_example-repacks.test_--_.part1.rar",
            "https://datanodes.to/c54bg7me2bvq/RuneScape_Dragonwilds_--_example-repacks.test_--_.part2.rar",
            "https://datanodes.to/3kt9aa4ldpuc/RuneScape_Dragonwilds_--_example-repacks.test_--_.part3.rar",
        ]

        tasks = []
        for url in urls:
            res = self.service.dispatch("add_task", {
                "url": url,
                "destination": str(self.downloads_dir),
            })
            tasks.append(res)

        t1, t2, t3 = tasks

        # Part 1 must be queued sentinel
        self.assertEqual(t1["state"], "queued")
        self.assertEqual(t1["package_part_number"], 1)

        # Parts 2 and 3 must be held in pending_probe
        self.assertEqual(t2["state"], "pending_probe")
        self.assertEqual(t2["package_part_number"], 2)
        self.assertEqual(t3["state"], "pending_probe")
        self.assertEqual(t3["package_part_number"], 3)

        # All parts must share the exact same package_key
        self.assertTrue(t1.get("package_key"))
        self.assertEqual(t1["package_key"], t2["package_key"])
        self.assertEqual(t2["package_key"], t3["package_key"])

    def test_e2e_concurrent_download_to_consensus_extraction_and_cleanup(self) -> None:
        """Sequential/concurrent part downloads hold extraction until all parts complete and verify."""
        pkg_folder = self.downloads_dir / "RuneScape Dragonwilds"
        pkg_folder.mkdir(parents=True, exist_ok=True)

        names = [
            "RuneScape_Dragonwilds_--_example-repacks.test_--_.part1.rar",
            "RuneScape_Dragonwilds_--_example-repacks.test_--_.part2.rar",
            "RuneScape_Dragonwilds_--_example-repacks.test_--_.part3.rar",
        ]

        # Write dummy archive volumes to disk
        part_paths = []
        for index, name in enumerate(names, 1):
            p = pkg_folder / name
            p.write_bytes(f"PART_CONTENT_{index}".encode("utf-8") * 100)
            part_paths.append(p)

        tasks = []
        package_key = "datanodes.to:runescape_dragonwilds_--_example-repacks.test_--_"
        for index, name in enumerate(names, 1):
            task = DownloadTask(
                f"https://datanodes.to/{index}/{name}",
                str(pkg_folder),
                id=f"e2e-task-{index}",
                display_name=name,
                state="downloading" if index > 1 else "completed",
                integrity_state="unverified",
                package_key=package_key,
                package_part_number=index,
                package_part_count=3,
                package_leader_id="e2e-task-1",
                resolved=[ResolvedItem("datanodes", f"https://datanodes.to/{index}/{name}", name,
                                       size=len(part_paths[index - 1].read_bytes()),
                                       relative_path=name)],
            )
            self.service.store.save(task)
            tasks.append(task)

        mock_backend = _MockArchiveWorkerBackend()
        self.service.archive_backend = mock_backend

        # Part 1 verifies, but Parts 2 and 3 are still downloading -> No extraction triggered
        tasks[0].integrity_state = "verified"
        self.service.store.save(tasks[0])
        asyncio.run(self.service._on_download_completed(tasks[0]))
        self.assertEqual(self.service.store.list_archive_jobs(), [], "Must not extract while siblings are incomplete")

        # Part 2 completes and verifies -> Still missing Part 3 -> No extraction
        tasks[1].state = "completed"
        tasks[1].integrity_state = "verified"
        self.service.store.save(tasks[1])
        asyncio.run(self.service._on_download_completed(tasks[1]))
        self.assertEqual(self.service.store.list_archive_jobs(), [], "Must not extract while Part 3 is incomplete")

        # Part 3 completes and verifies -> All 3 parts complete and verified -> Extraction triggers!
        tasks[2].state = "completed"
        tasks[2].integrity_state = "verified"
        self.service.store.save(tasks[2])
        asyncio.run(self.service._on_download_completed(tasks[2]))

        # Wait for archive job to complete
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            jobs = self.service.store.list_archive_jobs()
            if jobs and jobs[0]["state"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

        jobs = self.service.store.list_archive_jobs()
        self.assertEqual(len(jobs), 1, "Expected exactly 1 archive job")
        self.assertEqual(jobs[0]["state"], "completed", f"Job failed with: {jobs[0].get('error')}")

        # Check extracted payload exists on disk
        extracted_exe = pkg_folder / "RuneScape_Dragonwilds" / "game.exe"
        extracted_pak = pkg_folder / "RuneScape_Dragonwilds" / "data.pak"
        self.assertTrue(extracted_exe.is_file(), "Extracted game.exe must exist on disk")
        self.assertTrue(extracted_pak.is_file(), "Extracted data.pak must exist on disk")

        # Check that source archive volumes were cleaned up
        for p in part_paths:
            self.assertFalse(p.exists(), f"Source volume {p.name} must be deleted after verified extraction")

        # Verify task stage reached completed
        leader = self.service.store.get("e2e-task-1")
        self.assertEqual(leader.stage, "completed")

    def test_e2e_split_binary_join_and_cleanup(self) -> None:
        """Split binary chunks (.001, .002, .003) are merged into unified file and source chunks cleaned up."""
        pkg_folder = self.downloads_dir / "SplitPackage"
        pkg_folder.mkdir(parents=True, exist_ok=True)

        chunk1 = b"FIRST_SECTION_DATA_" * 50
        chunk2 = b"SECOND_SECTION_DATA_" * 50
        chunk3 = b"THIRD_SECTION_DATA_" * 50

        p1 = pkg_folder / "media.iso.001"
        p2 = pkg_folder / "media.iso.002"
        p3 = pkg_folder / "media.iso.003"
        p1.write_bytes(chunk1)
        p2.write_bytes(chunk2)
        p3.write_bytes(chunk3)

        expected_total = chunk1 + chunk2 + chunk3
        expected_sha = hashlib.sha256(expected_total).hexdigest()

        package_key = "direct:media.iso"
        names = ["media.iso.001", "media.iso.002", "media.iso.003"]
        tasks = []
        for index, (name, chunk) in enumerate([(names[0], chunk1), (names[1], chunk2), (names[2], chunk3)], 1):
            task = DownloadTask(
                f"https://direct.example/{name}",
                str(pkg_folder),
                id=f"split-task-{index}",
                display_name=name,
                state="completed",
                integrity_state="verified",
                package_key=package_key,
                package_part_number=index,
                package_part_count=3,
                package_leader_id="split-task-1",
                resolved=[ResolvedItem("direct", f"https://direct.example/{name}", name,
                                       size=len(chunk), relative_path=name)],
            )
            self.service.store.save(task)
            tasks.append(task)

        # Trigger completion on final task
        asyncio.run(self.service._on_download_completed(tasks[-1]))

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            jobs = self.service.store.list_archive_jobs()
            if jobs and jobs[0]["state"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

        jobs = self.service.store.list_archive_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["state"], "completed")

        joined_file = pkg_folder / "media.iso"
        self.assertTrue(joined_file.is_file(), "Joined file must exist on disk")
        self.assertEqual(joined_file.read_bytes(), expected_total)
        self.assertEqual(hashlib.sha256(joined_file.read_bytes()).hexdigest(), expected_sha)


if __name__ == "__main__":
    unittest.main()
