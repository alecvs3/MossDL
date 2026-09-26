import json
import tempfile
import unittest
from pathlib import Path

from engine.service import EngineService
from engine.errors import ProviderMappedError


FIXTURE = Path(__file__).parent / "crawling" / "fixtures" / "nested_cycle_partial.json"


class DurableCrawlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = EngineService(self.temp.name)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_nested_graph_is_bounded_hierarchical_and_restart_safe(self):
        fixture = json.loads(FIXTURE.read_text())
        secrets = {"Cookie": "fixture-cookie", "Authorization": "Bearer fixture-token", "password": "fixture-password"}
        result = self.service.dispatch("collection_create", {
            "id": "crawl-fixture", "url": fixture["source"], "provider_id": "fixture",
            "items": fixture["items"], "max_depth": 2, "max_pages": 5, "max_items": 5,
            "secrets": secrets,
        })
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["outcome"], "success")
        self.assertTrue(any(node["folder_path"] == "Package/Folder A" for node in result["nodes"]))
        self.assertTrue(any(node["parent_id"] for node in result["nodes"]))
        serialized = json.dumps(result) + json.dumps(self.service.store.list_crawl_events("crawl-fixture"))
        for secret in secrets.values():
            self.assertNotIn(secret, serialized)
        self.service.close()
        self.service = EngineService(self.temp.name)
        recovered = self.service.dispatch("crawl_get", {"id": "crawl-fixture"})
        self.assertEqual([node["node_id"] for node in recovered["nodes"]], [node["node_id"] for node in result["nodes"]])
        self.assertEqual(recovered["nodes"][0]["folder_path"], "Package/Folder A")

    def test_cancel_is_terminal_and_does_not_expand(self):
        result = self.service.dispatch("collection_create", {
            "id": "cancel-fixture", "url": "https://crawl.example.test/root",
            "items": [{"item_id": "one", "source_url": "https://example.test/one"}],
        })
        canceled = self.service.dispatch("crawl_cancel", {"id": result["id"]})
        self.assertEqual(canceled["outcome"], "canceled")
        continued = self.service.dispatch("collection_next_page", {"id": result["id"], "items": [{"item_id": "two", "source_url": "https://example.test/two"}]})
        self.assertEqual(continued["state"], "canceled")
        self.assertEqual(len(continued["items"]), 1)
        self.assertEqual(self.service.dispatch("collection_enqueue", {"id": result["id"], "item_ids": [result["items"][0]["stable_id"]], "destination": self.temp.name})["queued"], [])

    def test_quota_keeps_discovered_siblings_and_maps_provider_outcome(self):
        calls = []
        def enumerate_page(url, secrets):
            calls.append((url, secrets))
            if len(calls) == 1:
                return [{"item_id": "good", "source_url": "https://example.test/good", "children": ["https://example.test/next"]},
                        {"item_id": "other", "source_url": "https://example.test/other"}]
            raise ProviderMappedError("quota for sentinel-token", "quota")
        self.service.plugins.enumerate = enumerate_page
        result = self.service.dispatch("collection_create", {
            "id": "quota-fixture", "url": "https://crawl.example.test/root", "provider_id": "fixture",
            "max_pages": 4, "secrets": {"Cookie": "sentinel-cookie", "token": "sentinel-token"},
        })
        self.assertEqual(result["outcome"], "partial_failure")
        self.assertTrue(any(item["item_id"] == "good" for item in result["nodes"]))
        self.assertTrue(any(event["outcome"] == "quota" for event in result["events"]))
        self.assertEqual(calls[0][1]["token"], "sentinel-token")
        serialized = json.dumps(result) + json.dumps(self.service.store.list_crawl_events("quota-fixture"))
        self.assertNotIn("sentinel-token", serialized)
        self.assertNotIn("sentinel-cookie", serialized)

    def test_actionable_outcomes_and_secret_redaction_cover_snapshots_and_events(self):
        for category, expected in (("login_required", "login_required"), ("captcha", "captcha"), ("unsupported", "unsupported")):
            def enumerate_page(_url, _secrets, category=category):
                raise ProviderMappedError("requires " + category, category)
            self.service.plugins.enumerate = enumerate_page
            result = self.service.dispatch("collection_create", {
                "id": "outcome-" + category, "url": "https://crawl.example.test/" + category,
                "provider_id": "fixture", "secrets": {"password": "sentinel-password"},
            })
            self.assertEqual(result["outcome"], expected)
            serialized = json.dumps(result) + json.dumps(self.service.store.list_crawl_events("outcome-" + category))
            self.assertNotIn("sentinel-password", serialized)


if __name__ == "__main__":
    unittest.main()
