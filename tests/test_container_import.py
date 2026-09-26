import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from engine.container_import import ContainerImportError, normalize_import
from engine.http_api import required_scope
from engine.service import EngineService
from engine.cli import main


FIXTURES = Path(__file__).parent / "crawling" / "fixtures"


class ContainerImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = EngineService(self.temp.name)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_all_formats_normalize_and_preserve_provenance(self):
        results = []
        for filename in ("urls.txt", "batch.json", "sample.dlc", "sample.crawljob"):
            result = self.service.dispatch("container_import", {"path": str(FIXTURES / filename), "name": filename})
            results.append(result)
            self.assertEqual(result["import"]["source_name"], filename)
            self.assertGreater(result["import"]["new_count"], 0)
            self.assertTrue(any(node["metadata"]["source_format"] == result["import"]["format"] for node in result["nodes"]))
        self.assertEqual(len({result["id"] for result in results}), 4)

    def test_repeated_import_is_idempotent_and_restart_safe(self):
        payload = (FIXTURES / "batch.json").read_text()
        first = self.service.dispatch("container_import", {"content": payload, "format": "batch", "container_id": "stable"})
        second = self.service.dispatch("container_import", {"content": payload, "format": "batch", "container_id": "stable"})
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["import"]["new_count"], 0)
        self.assertEqual(len(first["nodes"]), len(second["nodes"]))
        self.service.close()
        self.service = EngineService(self.temp.name)
        recovered = self.service.dispatch("crawl_get", {"id": first["id"]})
        self.assertEqual({node["display_name"] for node in recovered["nodes"] if node["status"] == "discovered"}, {"a.zip", "b.zip"})
        self.assertTrue(any(node["status"] == "container" and node["folder_path"] == "Package/Folder A" for node in recovered["nodes"]))

    def test_secret_material_is_not_persisted(self):
        result = self.service.dispatch("container_import", {"format": "batch", "content": json.dumps({
            "items": [{"url": "https://files.example.test/x.zip?token=sentinel-token&signature=sentinel-signature",
                        "headers": {"Authorization": "Bearer sentinel-auth"}, "password": "sentinel-password"}]}),
            "container_id": "secret-safe"})
        serialized = json.dumps(result) + json.dumps(self.service.dispatch("crawl_events", {"id": result["id"]}))
        for secret in ("sentinel-token", "sentinel-signature", "sentinel-auth", "sentinel-password"):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("token=", serialized)

    def test_bounds_and_url_validation(self):
        with self.assertRaises(ContainerImportError):
            normalize_import("https://user:password@example.test/file")
        with self.assertRaises(ContainerImportError):
            normalize_import("https://example.test/one\nnot-a-url", format="text")
        with self.assertRaises(ContainerImportError):
            normalize_import("https://example.test/file", format="text", max_bytes=4)

    def test_authenticated_surface_scopes_and_no_unselected_tasks(self):
        self.assertEqual(required_scope("container_import"), "enqueue")
        self.assertEqual(required_scope("crawl_get"), "read")
        self.assertIsNone(required_scope("crawl_unknown"))
        result = self.service.dispatch("container_import", {"content": "https://files.example.test/one", "format": "text"})
        self.assertEqual(self.service.store.list(), [])
        self.assertEqual(self.service.dispatch("crawl_snapshot", {"id": result["id"]})["id"], result["id"])
        self.assertEqual(self.service.dispatch("crawl_continue", {"id": result["id"], "items": []})["id"], result["id"])
        self.assertEqual(self.service.dispatch("crawl_cancel", {"id": result["id"]})["outcome"], "canceled")

    def test_client_idempotency_and_cli_return_same_import_result(self):
        content = "https://files.example.test/idempotent.zip"
        first = self.service.dispatch("container_import", {"content": content, "format": "text", "idempotency_key": "client-1"})
        second = self.service.dispatch("container_import", {"content": content, "format": "text", "idempotency_key": "client-1"})
        self.assertEqual(first, second)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--data-dir", self.temp.name, "import", str(FIXTURES / "urls.txt")]), 0)
        cli_result = json.loads(output.getvalue())
        self.assertEqual(cli_result["import"]["format"], "text")
        self.assertIn("id", cli_result)


if __name__ == "__main__":
    unittest.main()
