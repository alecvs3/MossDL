import json
import tempfile
import unittest

from engine.errors import QuotaExceeded, classify_provider_failure
from engine.models import DownloadTask
from engine.service import EngineService


class ProviderFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = EngineService(self.temp.name)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_failure_categories_have_actionable_policy(self):
        result = classify_provider_failure(QuotaExceeded(provider_id="fixture", retry_after=12))
        self.assertEqual(result.category, "quota")
        self.assertTrue(result.retryable)
        self.assertEqual(result.retry_after, 12)

    def test_failure_transition_is_redacted_restart_safe_and_terminal_guarded(self):
        task = DownloadTask("https://fixture.example/file", self.temp.name, id="failure-task")
        self.service.store.save(task)
        result = self.service.dispatch("provider_failure", {
            "task_id": task.id, "provider_id": "fixture", "category": "captcha",
            "message": "captcha for sentinel-token", "challenge": {"site": "https://challenge.test", "token": "sentinel-token"},
        })
        self.assertEqual(result["category"], "captcha")
        self.assertEqual(self.service.store.get(task.id).state, "needs_user")
        payload = json.dumps(self.service.store.pending_events())
        self.assertNotIn("sentinel-token", payload)
        self.service.close()
        self.service = EngineService(self.temp.name)
        recovered = self.service.store.get(task.id)
        self.assertEqual(recovered.state, "needs_user")
        self.service.dispatch("provider_failure", {"task_id": task.id, "category": "quota", "message": "later"})
        self.assertEqual(self.service.store.get(task.id).state, "needs_user")

    def test_admission_rejects_disabled_account_without_invoking_provider(self):
        self.service.store.save_account({"id": "acct-disabled", "provider_id": "fixture",
                                         "credential_ref": "keychain://account/test", "enabled": False,
                                         "state": "disabled"})
        result = self.service.dispatch("provider_admission", {"provider_id": "fixture", "account_id": "acct-disabled"})
        self.assertFalse(result["admitted"])
        self.assertEqual(result["category"], "admission_rejected")

    def test_partial_collection_item_failure_keeps_siblings(self):
        result = self.service.dispatch("collection_create", {
            "id": "failure-crawl", "url": "https://fixture.example/root", "provider_id": "fixture",
            "items": [{"item_id": "good", "source_url": "https://fixture.example/good"},
                      {"item_id": "bad", "source_url": "https://fixture.example/bad"}],
        })
        bad = next(item["stable_id"] for item in result["items"] if item["source_url"].endswith("/bad"))
        outcome = self.service.dispatch("provider_failure", {"collection_id": result["id"], "node_id": bad,
                                                                "category": "unsupported", "message": "unsupported"})
        self.assertEqual(outcome["category"], "unsupported")
        current = self.service.dispatch("collection_get", {"id": result["id"]})
        self.assertEqual(len(current["items"]), 2)
        self.assertEqual(next(item["status"] for item in current["items"] if item["stable_id"] == bad), "unsupported")


if __name__ == "__main__":
    unittest.main()
