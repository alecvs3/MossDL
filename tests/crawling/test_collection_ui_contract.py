import json
import tempfile
import unittest
from pathlib import Path

from engine.service import EngineService


FIXTURE = Path(__file__).parent / "fixtures" / "nested_cycle_partial.json"
REVIEW_SOURCE = Path(__file__).parents[2] / "src" / "components" / "CollectionPlanReview.tsx"


class CollectionUiContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = EngineService(self.temp.name)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_selected_stable_items_become_exact_ordinary_tasks(self):
        fixture = json.loads(FIXTURE.read_text())
        plan = self.service.dispatch("collection_create", {
            "id": "ui-fixture", "url": fixture["source"], "provider_id": "fixture",
            "items": fixture["items"], "max_depth": 2, "max_items": 8,
        })
        leaf_items = [item for item in plan["items"] if item["source_url"].endswith("file-1.bin") or item["source_url"].endswith("other.bin")]
        self.assertGreaterEqual(len(leaf_items), 2)
        selected = [leaf_items[0]["stable_id"], leaf_items[1]["stable_id"]]
        result = self.service.dispatch("collection_enqueue", {"id": plan["id"], "item_ids": selected, "destination": self.temp.name})
        self.assertEqual([task["selected_item_ids"] for task in result["queued"]], [[selected[0]], [selected[1]]])
        self.assertEqual(len(self.service.store.list()), 2)
        task_ids = {task["id"] for task in result["queued"]}
        events = self.service.store.db.execute("SELECT event_type FROM event_outbox WHERE task_id IN (?, ?)", tuple(task_ids)).fetchall()
        self.assertEqual({event["event_type"] for event in events}, {"TaskCreated"})
        refreshed = self.service.dispatch("collection_get", {"id": plan["id"]})
        self.assertTrue(all(item["status"] == "queued" for item in refreshed["items"] if item["stable_id"] in selected))
        self.assertTrue(any(item["stable_id"] not in selected and item["status"] != "queued" for item in refreshed["items"]))

    def test_stale_id_rejected_before_any_task_or_lifecycle_change(self):
        plan = self.service.dispatch("collection_create", {
            "id": "stale-fixture", "url": "https://crawl.example.test/root", "items": [
                {"item_id": "one", "source_url": "https://cdn.example.test/one.bin"},
            ],
        })
        with self.assertRaisesRegex(ValueError, "stale or unknown"):
            self.service.dispatch("collection_enqueue", {"id": plan["id"], "item_ids": ["stale-node"], "destination": self.temp.name})
        self.assertEqual(self.service.store.list(), [])
        current = self.service.dispatch("collection_get", {"id": plan["id"]})
        self.assertEqual(current["state"], plan["state"])
        self.assertEqual(current["graph_revision"], plan["graph_revision"])

    def test_parent_selection_does_not_expand_and_enqueue_does_not_enumerate(self):
        calls = []
        self.service.plugins.resolve_chain = lambda *args, **kwargs: calls.append(args) or []
        plan = self.service.dispatch("collection_create", {
            "id": "parent-fixture", "url": "https://crawl.example.test/root", "items": [
                {"item_id": "parent", "source_url": "https://crawl.example.test/parent", "children": ["https://cdn.example.test/child.bin"]},
                {"item_id": "sibling", "source_url": "https://cdn.example.test/sibling.bin"},
            ],
        })
        parent = next(node for node in plan["nodes"] if node["item_id"] == "parent")
        with self.assertRaisesRegex(ValueError, "collection parent nodes"):
            self.service.dispatch("collection_enqueue", {"id": plan["id"], "item_ids": [parent["node_id"]], "destination": self.temp.name})
        self.assertEqual(calls, [])
        self.assertEqual(self.service.store.list(), [])

    def test_review_contract_preserves_ancestors_and_actionable_outcomes(self):
        source = REVIEW_SOURCE.read_text()
        self.assertIn("ancestorIds", source)
        for outcome in ("partial_failure", "quota", "captcha", "login_required", "unsupported", "cycle", "canceled"):
            self.assertIn(outcome, source)
        self.assertNotIn("setInterval", source)
        self.assertNotIn("progressBytes", source)


if __name__ == "__main__":
    unittest.main()
