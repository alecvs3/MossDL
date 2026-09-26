"""Lock-safety contracts for archive extraction, retry normalization and cleanup.

These tests pin the invariants that make source deletion safe:
  * an explicit retry can always return an active job to queued,
  * boot scheduling never hands the same job to the scheduler twice,
  * the archive job row persists the manifest/verification fields,
  * produced payload is distinguishable from staging/sentinel/source files,
  * deletion waits are bounded polls, and
  * the worker only marks completion after a fully promoted payload.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.archive_backend import ArchiveBackend, completion_sentinel_path
from engine.archive_joiner import produced_file_manifest
from engine.db import TaskStore
from engine.models import ArchiveJob, DownloadTask

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GENERIC_READ = 0x80000000
    _OPEN_EXISTING = 3
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    def _open_exclusive(path: Path):
        _KERNEL32.CreateFileW.restype = wintypes.HANDLE
        _KERNEL32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        handle = _KERNEL32.CreateFileW(str(path), _GENERIC_READ, 0, None, _OPEN_EXISTING, 0, None)
        if handle in (None, _INVALID_HANDLE):
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        return handle

    def _close_handle(handle) -> None:
        _KERNEL32.CloseHandle(handle)


class ArchiveRetryNormalizationTests(unittest.TestCase):
    def _store_with_job(self, temp: str, job_id: str, state: str, package_key: str) -> TaskStore:
        store = TaskStore(Path(temp) / "state.sqlite3")
        task = DownloadTask("https://fixture.example/file", temp, id=f"task-{job_id}")
        store.save(task)
        job = ArchiveJob(
            job_id, task.id, str(Path(temp) / "a.001"), temp,
            package_key=package_key, state=state, staging_path=str(Path(temp) / "staging"),
        )
        store.insert_or_get_archive_job(job)
        return store

    def test_retry_normalization_from_extracting(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store_with_job(temp, "job-extracting", "queued", "pkg-extracting")
            try:
                store.transition_archive_job("job-extracting", "running")
                store.transition_archive_job("job-extracting", "extracting")
                before = store.get_archive_job("job-extracting")
                normalized = store.normalize_archive_job_for_retry("job-extracting")
                self.assertEqual(normalized.state, "queued")
                self.assertIsNone(normalized.staging_path)
                self.assertGreater(normalized.revision, before.revision)
                persisted = store.get_archive_job("job-extracting")
                self.assertEqual(persisted.state, "queued")
                self.assertIsNone(persisted.staging_path)
                self.assertEqual(persisted.revision, normalized.revision)
            finally:
                store.close()

    def test_retry_normalization_from_cleanup_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store_with_job(temp, "job-cleanup", "queued", "pkg-cleanup")
            try:
                store.transition_archive_job("job-cleanup", "running")
                store.transition_archive_job("job-cleanup", "extracting")
                store.transition_archive_job("job-cleanup", "verifying")
                store.transition_archive_job("job-cleanup", "cleanup_pending")
                normalized = store.normalize_archive_job_for_retry("job-cleanup")
                self.assertEqual(normalized.state, "queued")
                self.assertIsNone(normalized.staging_path)
            finally:
                store.close()

    def test_normalize_leaves_completed_job_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store_with_job(temp, "job-completed", "queued", "pkg-completed")
            try:
                store.transition_archive_job("job-completed", "running")
                completed = store.transition_archive_job("job-completed", "verifying")
                completed = store.transition_archive_job("job-completed", "completed")
                unchanged = store.normalize_archive_job_for_retry("job-completed")
                self.assertEqual(unchanged.state, "completed")
                self.assertEqual(unchanged.revision, completed.revision)
            finally:
                store.close()

    def test_boot_list_returns_each_recovered_job_once(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store_with_job(temp, "job-boot", "queued", "pkg-boot")
            try:
                store.transition_archive_job("job-boot", "running")
                recovered = store.recover_archive_jobs()
                self.assertEqual([job.id for job in recovered], ["job-boot"])
                self.assertEqual(recovered[0].recovery_reason, "application_restart")
                boot_jobs = store.list_archive_jobs_for_boot()
                ids = [job.id for job in boot_jobs]
                self.assertEqual(ids.count("job-boot"), 1)
                # Boot scheduling is single-pass: recovering must not duplicate rows.
                self.assertEqual(len(ids), len(set(ids)))
            finally:
                store.close()

    def test_update_archive_job_persists_manifest_and_verification_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = TaskStore(root / "state.sqlite3")
            task = DownloadTask("https://fixture.example/file", temp, id="persist-task")
            store.save(task)
            job = ArchiveJob(
                "job-persist", task.id, str(root / "a.001"), str(root / "stale-out"),
                package_key="pkg-persist", state="queued", operation="extract",
            )
            store.insert_or_get_archive_job(job)
            manifest = [
                {"part_number": 1, "path": str(root / "a.001"), "size": 6},
                {"part_number": 2, "path": str(root / "a.002"), "size": 6},
            ]
            try:
                job.part_manifest = manifest
                job.expected_size = 12
                job.output_directory = str(root / "resolved-out")
                job.expected_digest = "ab" * 32
                updated = store.update_archive_job(job)
            finally:
                store.close()

            reopened = TaskStore(root / "state.sqlite3")
            try:
                persisted = reopened.get_archive_job("job-persist")
                self.assertEqual(persisted.part_manifest, manifest)
                self.assertEqual(persisted.expected_size, 12)
                self.assertEqual(persisted.output_directory, str(root / "resolved-out"))
                self.assertEqual(persisted.expected_digest, "ab" * 32)
                self.assertEqual(persisted.revision, updated.revision)
            finally:
                reopened.close()


class ProducedFileManifestTests(unittest.TestCase):
    def test_manifest_excludes_staging_sentinel_and_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            output.mkdir()
            payload = output / "movie.mkv"
            payload.write_bytes(b"payload")
            nested = output / "sub"
            nested.mkdir()
            (nested / "extra.bin").write_bytes(b"more")
            staging = output / ".transfer-archive-staging-job-1"
            staging.mkdir()
            (staging / "partial.bin").write_bytes(b"partial")
            (output / ".transfer-archive-complete-job-1").write_text("done")
            source = root / "movie.part1.rar"
            source.write_bytes(b"source")

            manifest = produced_file_manifest(output, exclude_paths=[source])

            names = {Path(entry["path"]).name for entry in manifest}
            self.assertEqual(names, {"movie.mkv", "extra.bin"})
            sizes = {Path(entry["path"]).name: entry["size"] for entry in manifest}
            self.assertEqual(sizes["movie.mkv"], 7)
            self.assertEqual(sizes["extra.bin"], 4)

    def test_manifest_is_empty_for_missing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(produced_file_manifest(Path(temp) / "missing"), [])


class PathReleaseTests(unittest.TestCase):
    def test_wait_for_path_release_returns_immediately_when_unlocked(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "payload.bin"
            path.write_bytes(b"x")
            started = time.monotonic()
            self.assertTrue(ArchiveBackend.wait_for_path_release([path], timeout=1.0))
            self.assertLess(time.monotonic() - started, 0.5)

    def test_wait_for_path_release_ignores_absent_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertTrue(
                ArchiveBackend.wait_for_path_release([Path(temp) / "gone.bin"], timeout=0.2))

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics are required")
    def test_wait_for_path_release_times_out_while_handle_is_held(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "locked.bin"
            path.write_bytes(b"x")
            handle = _open_exclusive(path)
            try:
                self.assertFalse(
                    ArchiveBackend.wait_for_path_release([path], timeout=0.3, poll_interval=0.02))
            finally:
                _close_handle(handle)
            self.assertTrue(ArchiveBackend.wait_for_path_release([path], timeout=1.0))

    def test_completion_sentinel_name_matches_worker_contract(self):
        # Rust `safe_job_id` replaces every non-alphanumeric byte with "_"; both
        # sides must agree or the service can never find the sentinel.
        sentinel = completion_sentinel_path("C:/out", "extract-42")
        self.assertEqual(sentinel.name, ".transfer-archive-complete-extract_42")


class ArchiveWorkerSentinelTests(unittest.TestCase):
    def setUp(self):
        self.backend = ArchiveBackend()
        if not self.backend.available():
            self.skipTest("archive-worker binary has not been built")
        base = Path(__file__).parent / ".test-artifacts" / "archive-lock"
        base.mkdir(parents=True, exist_ok=True)
        self.root = base / f"run-{time.time_ns()}"
        self.root.mkdir()

    def _bundle(self, name: str = "bundle.zip") -> Path:
        archive = self.root / name
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.writestr("payload/data.txt", b"verified payload")
        return archive

    def test_extract_writes_sentinel_and_retry_is_idempotent(self):
        archive = self._bundle()
        output = self.root / "out"
        first = self.backend.extract(archive, output, job_id="sentinel-job")
        if "status" not in first:
            self.skipTest("archive-worker binary is stale; structured status/sentinel unavailable")
        self.assertEqual(first["status"], "completed")
        self.assertTrue(first["staging_removed"])
        self.assertTrue(completion_sentinel_path(output, "sentinel-job").is_file())

        second = self.backend.extract(archive, output, job_id="sentinel-job")
        self.assertEqual(second["files"], 1)
        self.assertGreaterEqual(second["skipped_existing_files"], 1)
        self.assertEqual((output / "payload" / "data.txt").read_bytes(), b"verified payload")

    def test_conflicting_payload_is_refused_and_sentinel_absent(self):
        archive = self._bundle("conflict.zip")
        output = self.root / "conflict-out"
        output.mkdir()
        payload_dir = output / "payload"
        payload_dir.mkdir()
        (payload_dir / "data.txt").write_bytes(b"tampered payload")

        with self.assertRaises(RuntimeError):
            self.backend.extract(archive, output, job_id="conflict-job")

        self.assertEqual((payload_dir / "data.txt").read_bytes(), b"tampered payload")
        self.assertFalse(completion_sentinel_path(output, "conflict-job").exists())
        staging = output.parent / ".transfer-archive-staging-conflict-job"
        self.assertFalse(staging.exists(), "failed extraction must clean its staging tree")


if __name__ == "__main__":
    unittest.main()
