from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import shutil
import sqlite3
import urllib.error
import unittest
from unittest.mock import patch
from pathlib import Path

from engine.db import RevisionConflict, TaskStore
from engine.models import DownloadTask, ResolvedItem
from engine.reliability import FailureClass, classify_failure, decide_retry, verify_file
from engine.secrets import InMemorySecretBackend, SecretManager, SecretRef
from engine.limits import AdaptiveTransferController
from engine.errors import ProviderMappedError
from engine.providers.mega import MegaProvider
from engine.service import EngineService


class ReliabilityPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / ".test-artifacts" / "reliability-platform" / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_integrity_rejects_html_error_without_content_type(self) -> None:
        path = self.root / "payload.bin"
        path.write_bytes(b"<!doctype html><html><body>rate limited</body></html>")
        report = verify_file(path)
        self.assertEqual(report.state, "provider_rejected")

    def test_compare_and_save_rejects_stale_task_revision(self) -> None:
        store = TaskStore(self.root / "revision.sqlite3")
        try:
            task = DownloadTask("https://site.test/file", str(self.root))
            store.save(task)
            stale = store.get(task.id)
            current = store.get(task.id)
            current.state = "resolving"
            store.save(current)
            stale.state = "failed"
            with self.assertRaises(RevisionConflict):
                store.save_if_revision(stale, stale.revision)
        finally:
            store.close()

    def test_task_revision_history_and_public_redaction(self) -> None:
        store = TaskStore(self.root / "tasks.sqlite3")
        try:
            task = DownloadTask("https://site.test/page", str(self.root), request_headers={"Authorization": "secret"})
            task.resolved = [ResolvedItem("p", task.source_url, "file.bin", direct_url="https://cdn.test/file?sig=secret")]
            store.save(task)
            task.state = "downloading"
            store.save_with_event(task, "TaskStarted", task.to_dict(), from_state="queued")
            public = store.get(task.id).to_dict()
            self.assertEqual(public["resolved"][0]["direct_url"], None)
            self.assertEqual(public["request_headers"], {})
            self.assertNotIn("secret", json.dumps(public))
            row = store.db.execute("SELECT resolved_json,request_headers_json FROM tasks WHERE id=?", (task.id,)).fetchone()
            self.assertNotIn("sig=secret", row[0])
            self.assertNotIn("Authorization", row[1])
            self.assertGreaterEqual(task.revision, 2)
            self.assertEqual(store.list_transitions(task.id)[0]["to_state"], "downloading")
        finally:
            store.close()

    def test_stale_recovery_and_idempotent_add_task(self) -> None:
        service = EngineService(self.root)
        try:
            first = service.dispatch("add_task", {"url": "https://example.test/a", "idempotency_key": "same"})
            second = service.dispatch("add_task", {"url": "https://example.test/a", "idempotency_key": "same"})
            self.assertEqual(first["id"], second["id"])
            task = service.store.get(first["id"])
            task.state, task.heartbeat_at = "downloading", 0
            service.store.save(task)
            self.assertEqual(service.store.recover_stale_tasks(1), [task.id])
            self.assertEqual(service.store.get(task.id).state, "queued")
        finally:
            service.close()

    def test_account_selection_quota_and_secret_reference_validation(self) -> None:
        service = EngineService(self.root)
        try:
            with self.assertRaises(ValueError):
                service.dispatch("account_create", {"provider_id": "p", "credential_ref": "raw-token"})
            service.dispatch("account_create", {"id": "p-a", "provider_id": "p", "credential_ref": "keychain://a", "state": "healthy"})
            service.dispatch("account_create", {"id": "p-b", "provider_id": "p", "credential_ref": "keychain://b", "state": "unknown", "used_bytes": 100, "quota_bytes": 100})
            selected = service.dispatch("account_select", {"provider_id": "p"})
            self.assertEqual(selected["id"], "p-a")
            quota = service.dispatch("account_quota", {"provider_id": "p", "account_id": "p-a"})
            self.assertIsNone(quota["remaining_bytes"])
            self.assertNotIn("raw-token", json.dumps(service.dispatch("list_accounts", {})))
        finally:
            service.close()

    def test_linkgrabber_bulk_retry_and_unsafe_rejection(self) -> None:
        service = EngineService(self.root)
        try:
            entries = service.dispatch("linkgrabber_add", {"text": "https://a.test/x\nhttps://b.test/y\njavascript:bad"})
            self.assertEqual(len(entries), 2)
            ids = [entry["id"] for entry in entries]
            self.assertEqual(service.dispatch("linkgrabber_bulk_update", {"ids": ids, "selected": True})["updated"], 2)
            service.dispatch("linkgrabber_bulk_update", {"ids": [ids[0]], "state": "failed", "error": "temporary"})
            self.assertEqual(service.dispatch("linkgrabber_retry", {"ids": [ids[0]]})["retried"], 1)
            self.assertEqual(service.store.list_links()[0]["state"], "pending")
        finally:
            service.close()

    def test_retry_classification_and_integrity_report(self) -> None:
        self.assertEqual(classify_failure("server returned HTTP 429", 429), FailureClass.RATE_LIMITED)
        decision = decide_retry("signed URL expired", 0)
        self.assertTrue(decision.retry)
        self.assertTrue(decision.refresh_session)
        target = self.root / "file.bin"
        target.write_bytes(b"hello")
        report = verify_file(target, expected_size=5, expected_checksum="2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
        self.assertEqual(report.state, "verified")
        self.assertEqual(verify_file(target, expected_size=4).state, "corrupt")

    def test_platform_workflow_api_and_backup(self) -> None:
        service = EngineService(self.root)
        try:
            self.assertEqual(service.dispatch("api_info", {})["version"], "v1")
            self.assertEqual(service.dispatch("worker_hello", {"worker_id": "w1", "capabilities": ["download"]})["worker_id"], "w1")
            job = service.dispatch("workflow_create", {"id": "job-1", "kind": "resolve", "steps": [{"id": "resolve"}]})
            self.assertEqual(service.dispatch("workflow_transition", {"id": "job-1", "state": "running"})["state"], "running")
            self.assertEqual(service.dispatch("workflow_get", {"id": "job-1"})["revision"], 2)
            backup = service.dispatch("backup_create", {"path": str(self.root / "backup.sqlite3")})
            self.assertTrue(Path(backup["path"]).is_file())
            self.assertIn("workflow_jobs", backup["migration"]["tables"])
        finally:
            service.close()

    def test_secret_manager_only_returns_opaque_references(self) -> None:
        backend = InMemorySecretBackend()
        manager = SecretManager(backend)
        reference = manager.put("bearer-value", kind="oauth")
        self.assertTrue(reference.startswith("keychain://oauth/"))
        self.assertEqual(manager.resolve(reference), "bearer-value")
        self.assertEqual(manager.validate(reference), reference)
        self.assertNotIn("bearer-value", reference)
        manager.rotate(reference, "rotated")
        self.assertEqual(manager.resolve(reference), "rotated")
        manager.delete(reference)
        self.assertIsNone(manager.resolve(reference))
        with self.assertRaises(ValueError):
            SecretRef.parse("https://raw-secret")

    def test_worker_lease_heartbeat_and_backup_restore(self) -> None:
        service = EngineService(self.root)
        try:
            worker = service.dispatch("worker_hello", {"worker_id": "worker-1", "capabilities": ["download"],
                                                        "metadata": {"token": "must-not-persist"}})
            self.assertEqual(worker["worker_id"], "worker-1")
            self.assertNotIn("must-not-persist", json.dumps(service.dispatch("worker_list", {})))
            leased = service.dispatch("worker_lease", {"worker_id": "worker-1", "lease_id": "lease-1"})
            self.assertEqual(leased["state"], "leased")
            self.assertTrue(service.dispatch("worker_heartbeat", {"worker_id": "worker-1", "lease_id": "lease-1"})["updated"])
            self.assertTrue(service.dispatch("worker_release", {"worker_id": "worker-1", "lease_id": "lease-1"})["released"])
            backup = self.root / "backup.sqlite3"
            service.dispatch("backup_create", {"path": str(backup)})
            restored = self.root / "restored.sqlite3"
            result = service.dispatch("backup_restore", {"source": str(backup), "destination": str(restored)})
            self.assertTrue(Path(result["path"]).is_file())
            restored_store = TaskStore(restored)
            try:
                self.assertIn("workers", restored_store.migration_status()["tables"])
            finally:
                restored_store.close()
        finally:
            service.close()

    def test_account_refresh_state_quarantine_and_priority(self) -> None:
        service = EngineService(self.root)
        try:
            service.dispatch("account_create", {"id": "low", "provider_id": "p", "credential_ref": "keychain://low",
                                                 "state": "healthy", "priority": 1})
            service.dispatch("account_create", {"id": "high", "provider_id": "p", "credential_ref": "keychain://high",
                                                 "state": "healthy", "priority": 10})
            selected = service.dispatch("account_select", {"provider_id": "p"})
            self.assertEqual(selected["id"], "high")
            service.store.update_account_refresh("high", "refreshing", quarantine_until=9999999999)
            self.assertEqual(service.dispatch("account_select", {"provider_id": "p"})["id"], "low")
            public = service.dispatch("list_accounts", {})
            self.assertEqual(next(item for item in public if item["id"] == "high")["refresh_state"], "refreshing")
        finally:
            service.close()

    def test_api_client_tokens_are_hashed_and_scoped(self) -> None:
        service = EngineService(self.root)
        try:
            result = service.dispatch("client_register", {"id": "read-only", "label": "Test client", "scopes": ["read"]})
            self.assertTrue(result["token"])
            self.assertNotIn(result["token"], json.dumps(service.dispatch("client_list", {})))
            token_hash = __import__("hashlib").sha256(result["token"].encode()).hexdigest()
            self.assertIsNotNone(service.store.find_api_client_by_hash(token_hash))
            service.dispatch("client_revoke", {"id": "read-only"})
            self.assertIsNone(service.store.find_api_client_by_hash(token_hash))
        finally:
            service.close()

    def test_adaptive_controller_warms_up_and_backs_off(self) -> None:
        controller = AdaptiveTransferController(global_concurrency=4, default_concurrency=2)
        permit = controller.acquire("google-drive:account-1:drive.google.com", ceiling=4, requests_per_second=0)
        permit.finish(True)
        for _ in range(4):
            current = controller.acquire("google-drive:account-1:drive.google.com", ceiling=4, requests_per_second=0)
            current.finish(True)
        warmed = controller.snapshot()[0]
        self.assertEqual(warmed["concurrency"], 3)
        failed = controller.acquire("google-drive:account-1:drive.google.com", ceiling=4, requests_per_second=0)
        failed.finish(False, status_code=429, retry_after=10)
        backed_off = controller.snapshot()[0]
        self.assertEqual(backed_off["concurrency"], 2)
        self.assertGreaterEqual(backed_off["cooldown_remaining"], 9)

    def test_provider_manifest_limits_feed_shared_admission(self) -> None:
        service = EngineService(self.root)
        try:
            self.assertEqual(service.resources.policy.provider_limits["google-drive"], 2)
            self.assertEqual(service.resources.provider_rates["google-drive"], 4.0)
            status = service.dispatch("adaptive_transfer_status", {})
            self.assertTrue(any(item["key"] == "google-drive:global:unknown" for item in status["controllers"]))
        finally:
            service.close()

    def test_mega_connection_failure_is_classified_for_retry(self) -> None:
        with patch("engine.route_http.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with self.assertRaises(ProviderMappedError) as raised:
                MegaProvider._api({"a": "f"}, "public-folder")
        self.assertEqual(raised.exception.category, "transient_network")
        self.assertIn("MEGA API is unreachable", str(raised.exception))

    @unittest.skipUnless((_ROOT / "hars" / "mega.har").is_file(), "needs the local MEGA capture (hars/, not published)")
    def test_mega_public_request_forms_match_reference_and_har(self) -> None:
        direct, direct_context = MegaProvider._download_request("file-node")
        child, child_context = MegaProvider._download_request("child-node", "folder-node")
        self.assertEqual(direct, {"a": "g", "p": "file-node", "g": 1})
        self.assertIsNone(direct_context)
        self.assertEqual(child, {"a": "g", "n": "child-node", "g": 1})
        self.assertEqual(child_context, "folder-node")

        har = json.loads((_ROOT / "hars" / "mega.har").read_text(encoding="utf-8"))
        api_entries = [entry for entry in har["log"]["entries"]
                       if "g.api.mega.co.nz" in entry.get("request", {}).get("url", "")]
        self.assertTrue(api_entries)
        reference_body = json.loads(api_entries[-1]["request"]["postData"]["text"])[0]
        self.assertEqual(reference_body["a"], "g")
        self.assertIn("p", reference_body)

    def test_verify_file_skips_disk_hash_when_checksumless(self) -> None:
        target = self.root / "large_dummy.bin"
        target.write_bytes(b"A" * 1024 * 1024)
        report = verify_file(target, expected_size=1024 * 1024, expected_checksum=None)
        # No checksum was supplied and the disk was deliberately not rehashed, so
        # only the length was ever checked -- `size_verified`, not `verified`.
        self.assertEqual(report.state, "size_verified")
        self.assertIsNone(report.observed_checksum)

    def test_verify_file_reuses_observed_checksum_without_rehashing(self) -> None:
        target = self.root / "sample_precalculated.bin"
        target.write_bytes(b"hello world")
        in_flight_hash = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        report = verify_file(
            target,
            expected_size=11,
            expected_checksum=in_flight_hash,
            observed_checksum=in_flight_hash,
        )
        self.assertEqual(report.state, "verified")
        self.assertEqual(report.observed_checksum, in_flight_hash)

    def test_verify_file_detects_html_error_payload(self) -> None:
        target = self.root / "download.zip"
        target.write_bytes(b"<!DOCTYPE html><html><body>Error 403 Forbidden Cloudflare</body></html>")
        report = verify_file(target, expected_size=None, expected_checksum=None)
        self.assertEqual(report.state, "provider_rejected")
        self.assertIn("HTML error page", report.reason or "")


if __name__ == "__main__":
    unittest.main()
