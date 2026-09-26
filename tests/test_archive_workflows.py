from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from engine.db import RevisionConflict, TaskStore
from engine.archive_backend import completion_sentinel_path
from engine.errors import DownloadCanceled, NeedsUser
from engine.models import ArchiveJob, DownloadTask, ResolvedItem
from engine.secrets import InMemorySecretBackend, SecretManager
from engine.service import EngineService


class _FakeArchiveBackend:
    def __init__(self, *, needs_user: bool = False, cancellable: bool = False):
        self.needs_user = needs_user
        self.cancellable = cancellable
        self.calls = 0

    def extract(self, path, output_directory, password_ref=None, policy=None, control=None, job_id=None):
        self.calls += 1
        if self.needs_user:
            raise NeedsUser("archive password is required", action="archive_password")
        if self.cancellable and control and control.cancel.is_set():
            raise DownloadCanceled()
        output = Path(output_directory)
        output.mkdir(parents=True, exist_ok=True)
        (output / "safe.txt").write_bytes(b"safe output")
        if job_id:
            completion_sentinel_path(output, job_id).write_bytes(b"")
        return {"output_directory": str(output), "files": 1, "bytes": 11}


class _EmptyArchiveBackend:
    """Simulates a worker that reports success but produced no files on disk."""

    def extract(self, path, output_directory, password_ref=None, policy=None, control=None, job_id=None):
        output = Path(output_directory)
        output.mkdir(parents=True, exist_ok=True)
        return {"output_directory": str(output), "files": 0, "bytes": 0}


class ArchiveWorkflowTests(unittest.TestCase):
    def test_durable_manifest_defers_archive_until_all_parts_verify(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            names = ["demo.iso.001", "demo.iso.002", "demo.iso.003"]
            tasks = []
            service = EngineService(root)
            try:
                for index, name in enumerate(names, 1):
                    path = root / name
                    path.write_bytes(bytes([index]) * 6)
                    task = DownloadTask(
                        f"https://fixture.example/{name}", str(root), id=f"manifest-{index}",
                        display_name=name, state="completed" if index == 1 else "queued",
                        integrity_state="verified" if index == 1 else "unverified",
                        package_key="fixture:demo", package_part_number=index,
                        package_part_count=3, package_leader_id="manifest-1",
                        resolved=[ResolvedItem("fixture", f"https://fixture.example/{name}", name,
                                               size=6, relative_path=name)],
                    )
                    service.store.save(task)
                    tasks.append(task)

                asyncio.run(service._on_download_completed(tasks[0]))
                self.assertEqual(service.store.list_archive_jobs(), [])

                for task in tasks:
                    task.state = "completed"
                    task.integrity_state = "verified"
                    service.store.save(task)
                asyncio.run(service._on_download_completed(tasks[-1]))

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    current = service.store.list_archive_jobs()
                    if current and current[0]["state"] in {"completed", "failed"}:
                        break
                    time.sleep(0.02)
                jobs = service.store.list_archive_jobs()
                self.assertEqual(len(jobs), 1)
                self.assertEqual(len(json.loads(jobs[0]["part_manifest_json"])), 3)
            finally:
                service.close()

    def test_completed_multipart_triggers_one_restart_safe_join(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "demo.iso.001").write_bytes(b"first-")
            (root / "demo.iso.002").write_bytes(b"second")
            service = EngineService(root)
            try:
                task = DownloadTask(
                    "https://fixture.example/demo.iso.001", str(root), id="multipart-task", state="completed",
                    resolved=[
                        ResolvedItem("fixture", "https://fixture.example/1", "demo.iso.001", size=6,
                                     relative_path="demo.iso.001"),
                        ResolvedItem("fixture", "https://fixture.example/2", "demo.iso.002", size=6,
                                     relative_path="demo.iso.002"),
                    ],
                )
                service.store.save(task)
                asyncio.run(service._on_download_completed(task))
                asyncio.run(service._on_download_completed(task))

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    jobs = service.store.list_archive_jobs(task.id)
                    if jobs and jobs[0]["state"] == "completed":
                        break
                    time.sleep(0.02)

                jobs = service.store.list_archive_jobs(task.id)
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]["state"], "completed")
                self.assertEqual((root / "demo.iso").read_bytes(), b"first-second")
                queued = [event for event in service.store.events_since() if event["event_type"] == "ArchiveQueued"]
                self.assertEqual(len(queued), 1)
                self.assertEqual(service.store.get(task.id).state, "completed")
            finally:
                service.close()

    def test_archive_job_transitions_are_guarded_and_restart_recovery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            store = TaskStore(Path(temp) / "state.sqlite3")
            task = DownloadTask("https://fixture.example/file", temp, id="archive-task")
            store.save(task)
            job = ArchiveJob(
                "job-1", task.id, str(Path(temp) / "a.001"), temp,
                package_key="package-1", part_manifest=[], expected_size=0,
            )
            stored, created = store.insert_or_get_archive_job(job)
            self.assertTrue(created)
            replay, replay_created = store.insert_or_get_archive_job(
                ArchiveJob("job-2", task.id, job.input_path, temp, package_key="package-1")
            )
            self.assertFalse(replay_created)
            self.assertEqual(replay.id, stored.id)
            store.transition_archive_job(stored.id, "running")
            store.transition_archive_job(stored.id, "verifying")
            store.transition_archive_job(stored.id, "completed")
            with self.assertRaises(ValueError):
                store.transition_archive_job(stored.id, "queued")
            store.close()

            recovered_store = TaskStore(Path(temp) / "state.sqlite3")
            try:
                self.assertEqual(recovered_store.recover_archive_jobs(), [])
                self.assertEqual(recovered_store.get_archive_job(stored.id).state, "completed")
            finally:
                recovered_store.close()

    def test_archive_job_history_survives_task_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            store = TaskStore(Path(temp) / "state.sqlite3")
            task = DownloadTask("https://fixture.example/file", temp, id="deleted-task")
            store.save(task)
            job = ArchiveJob(
                "job-survives-delete", task.id, str(Path(temp) / "a.001"), temp,
                package_key="package-survives-delete", state="failed",
                error="fixture failure",
            )
            store.insert_or_get_archive_job(job)
            store.delete(task.id)
            rows = store.list_archive_jobs(task.id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], job.id)
            self.assertEqual(rows[0]["state"], "failed")
            self.assertEqual(rows[0]["task_id"], task.id)
            store.close()

    def test_running_archive_job_reopens_as_queued_with_recovery_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            store = TaskStore(Path(temp) / "state.sqlite3")
            task = DownloadTask("https://fixture.example/file", temp, id="archive-recovery-task")
            store.save(task)
            job = ArchiveJob("job-recovery", task.id, str(Path(temp) / "a.001"), temp, package_key="package-recovery")
            store.insert_or_get_archive_job(job)
            store.transition_archive_job(job.id, "running")
            recovered = store.recover_archive_jobs()
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].state, "queued")
            self.assertEqual(recovered[0].recovery_reason, "application_restart")
            with self.assertRaises(RevisionConflict):
                store.transition_archive_job(recovered[0].id, "running", expected_revision=0)
            store.close()

    def test_digest_mismatch_fails_without_promoting_staging_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "bad.iso.001"
            second = root / "bad.iso.002"
            first.write_bytes(b"first-")
            second.write_bytes(b"second")
            service = EngineService(root)
            try:
                task = DownloadTask("https://fixture.example/bad.iso.001", str(root), id="digest-task")
                service.store.save(task)
                job = ArchiveJob(
                    "digest-job", task.id, str(first), str(root), format="split_binary",
                    package_key="digest-package", part_manifest=[
                        {"part_number": 1, "path": str(first), "size": first.stat().st_size},
                        {"part_number": 2, "path": str(second), "size": second.stat().st_size},
                    ], expected_size=first.stat().st_size + second.stat().st_size,
                    expected_digest=hashlib.sha256(b"wrong").hexdigest(),
                )
                service.store.insert_or_get_archive_job(job)
                asyncio.run(service._run_archive_job(job.id))

                failed = service.store.get_archive_job(job.id)
                self.assertIsNotNone(failed)
                self.assertEqual(failed.state, "failed")
                self.assertFalse((root / "bad.iso").exists())
                self.assertFalse((root / ".bad.iso.joining_tmp").exists())
            finally:
                service.close()

    def test_durable_archive_extract_promotes_only_after_safe_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "payload.zip"
            source.write_bytes(b"archive fixture")
            service = EngineService(root)
            service.archive_backend = _FakeArchiveBackend()
            try:
                task = DownloadTask("https://fixture.example/payload.zip", str(root), id="extract-task", state="completed")
                service.store.save(task)
                output = root / "extracted"
                job = ArchiveJob(
                    "extract-job", task.id, str(source), str(output), format="zip",
                    package_key="extract-package", operation="extract",
                )
                service.store.insert_or_get_archive_job(job)
                asyncio.run(service._run_archive_job(job.id))

                completed = service.store.get_archive_job(job.id)
                self.assertEqual(completed.state, "completed")
                self.assertEqual(completed.observed_size, 11)
                self.assertEqual((output / "safe.txt").read_bytes(), b"safe output")
                self.assertEqual(service.store.get(task.id).state, "completed")
                event_types = [event["event_type"] for event in service.store.events_since()
                               if event["task_id"] == task.id]
                self.assertIn("ArchiveStarted", event_types)
                self.assertIn("ArchiveProgress", event_types)
                self.assertIn("ArchiveCompleted", event_types)
            finally:
                service.close()

    def test_extraction_without_output_fails_and_preserves_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "payload.zip"
            source.write_bytes(b"archive fixture")
            service = EngineService(root)
            service.archive_backend = _EmptyArchiveBackend()
            try:
                task = DownloadTask("https://fixture.example/payload.zip", str(root), id="empty-task", state="completed")
                service.store.save(task)
                output = root / "extracted"
                job = ArchiveJob(
                    "empty-job", task.id, str(source), str(output), format="zip",
                    package_key="empty-package", operation="extract",
                )
                service.store.insert_or_get_archive_job(job)
                asyncio.run(service._run_archive_job(job.id))

                failed = service.store.get_archive_job(job.id)
                self.assertEqual(failed.state, "failed")
                # Sources must never be deleted when verification fails.
                self.assertTrue(source.exists())
            finally:
                service.close()

    def test_extraction_with_output_deletes_sources_after_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "payload.zip"
            source.write_bytes(b"archive fixture")
            service = EngineService(root)
            service.archive_backend = _FakeArchiveBackend()
            try:
                task = DownloadTask("https://fixture.example/payload.zip", str(root), id="cleanup-task", state="completed")
                service.store.save(task)
                output = root / "extracted"
                job = ArchiveJob(
                    "cleanup-job", task.id, str(source), str(output), format="zip",
                    package_key="cleanup-package", operation="extract",
                )
                service.store.insert_or_get_archive_job(job)
                asyncio.run(service._run_archive_job(job.id))

                completed = service.store.get_archive_job(job.id)
                self.assertEqual(completed.state, "completed")
                self.assertTrue((output / "safe.txt").exists())
                # Source is removed only after verified extraction.
                self.assertFalse(source.exists())
            finally:
                service.close()

    def test_multipart_auto_extract_off_emits_one_durable_choice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "Game.part01.rar"
            second = root / "Game.part02.rar"
            first.write_bytes(b"part-one")
            second.write_bytes(b"part-two")
            service = EngineService(root)
            try:
                task = DownloadTask(
                    "https://datanodes.to/file/Game.part01.rar",
                    str(root),
                    id="choice-task",
                    state="completed",
                    browser_context={"auto_extract": False},
                    resolved=[ResolvedItem(
                        provider="datanodes",
                        source_url="https://datanodes.to/file/Game.part01.rar",
                        display_name=first.name,
                        size=first.stat().st_size,
                    )],
                )
                service.store.save(task)

                asyncio.run(service._on_download_completed(task))
                asyncio.run(service._on_download_completed(task))

                choices = [
                    event for event in service.store.events_since()
                    if event["event_type"] == "ArchiveChoiceRequired"
                ]
                self.assertEqual(len(choices), 1)
                self.assertEqual(choices[0]["payload"]["expected_size"], first.stat().st_size + second.stat().st_size)
                self.assertEqual(service.store.list_archive_jobs(task.id), [])
            finally:
                service.close()

    def test_archive_extract_rejects_relative_destination_before_worker_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "payload.zip"
            source.write_bytes(b"archive fixture")
            service = EngineService(root)
            fake = _FakeArchiveBackend()
            service.archive_backend = fake
            try:
                task = DownloadTask("https://fixture.example/payload.zip", str(root), id="unsafe-extract-task", state="completed")
                service.store.save(task)
                job = ArchiveJob(
                    "unsafe-extract-job", task.id, str(source), "relative-output", format="zip",
                    package_key="unsafe-extract-package", operation="extract",
                )
                service.store.insert_or_get_archive_job(job)
                asyncio.run(service._run_archive_job(job.id))
                failed = service.store.get_archive_job(job.id)
                self.assertEqual(failed.state, "failed")
                self.assertEqual(fake.calls, 0)
                self.assertFalse((root / "relative-output").exists())
            finally:
                service.close()

    def test_archive_password_requirement_is_durable_and_redacted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "private.zip"
            source.write_bytes(b"archive fixture")
            service = EngineService(root)
            service.secrets = SecretManager(InMemorySecretBackend())
            password_ref = service.secrets.put("correct horse battery staple", kind="archive")
            service.archive_backend = _FakeArchiveBackend(needs_user=True)
            try:
                task = DownloadTask("https://fixture.example/private.zip", str(root), id="password-task", state="completed")
                service.store.save(task)
                job = ArchiveJob(
                    "password-extract-job", task.id, str(source), str(root / "output"), format="zip",
                    password_ref=password_ref, package_key="password-extract-package", operation="extract",
                )
                service.store.insert_or_get_archive_job(job)
                asyncio.run(service._run_archive_job(job.id))
                waiting = service.store.get_archive_job(job.id)
                self.assertEqual(waiting.state, "needs_user")
                self.assertEqual(waiting.password_ref, password_ref)
                serialized = str(service.store.events_since()).lower()
                self.assertNotIn("correct horse battery staple", serialized)
                needs_user = [event for event in service.store.events_since()
                              if event["event_type"] == "ArchiveNeedsUser"]
                self.assertEqual(len(needs_user), 1)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
