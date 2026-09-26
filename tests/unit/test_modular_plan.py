from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import unittest
import zipfile
from pathlib import Path

from engine.db import TaskStore
from engine.fallback import ProviderFallback, route_error_is_switchable, source_fingerprint
from engine.models import DownloadTask, ResolvedItem
from engine.paths import validate_directory
from engine.plugin_catalog import ManifestError, loads_jsonc, validate_manifest
from engine.routes import RouteManager
from engine.plugins import PluginRegistry
from engine.service import EngineService


class ModularPlanTests(unittest.TestCase):
    artifacts = Path(__file__).parent / ".test-artifacts" / "modular"

    @classmethod
    def setUpClass(cls):
        cls.artifacts.mkdir(parents=True, exist_ok=True)

    def test_jsonc_comments_trailing_commas_and_duplicate_keys(self):
        value = loads_jsonc('{"id":"demo", // comment\n "items":[1,2,],}')
        self.assertEqual(value["items"], [1, 2])
        with self.assertRaises(ManifestError):
            loads_jsonc('{"id":"demo", "id":"again"}')

    def test_manifest_validation_rejects_regex_and_capability(self):
        base = {"schema_version": 1, "id": "demo", "version": "1.0.0", "hosts": ["demo.test"],
                "match": {"path_regex": "["}, "capabilities": {}, "limits": {}, "hooks": {}}
        with self.assertRaises(ManifestError):
            validate_manifest(base)
        base["match"] = {}
        base["capabilities"] = {"telepathy": True}
        with self.assertRaises(ManifestError):
            validate_manifest(base)

    def test_builtin_manifest_host_matching_and_route_secret_boundary(self):
        registry = PluginRegistry()
        try:
            self.assertEqual(registry.provider_for("https://transfer.it/t/example"), "transfer.it")
            self.assertEqual(registry.provider_for("https://not-transfer.it/t/example"), "generic")
        finally:
            registry.close()
        with self.assertRaises(ValueError):
            RouteManager.validate({"id": "bad", "kind": "socks5", "endpoint": "socks5://user:pass@127.0.0.1:1080"})

    def test_absolute_and_relative_directories_are_persistable(self):
        base = self.artifacts / "paths"
        base.mkdir(parents=True, exist_ok=True)
        absolute = base / "other-drive" / "downloads"
        result = validate_directory(str(absolute), base, create=True)
        self.assertTrue(result["valid"], result)
        self.assertEqual(Path(result["path"]), absolute.resolve())
        relative = validate_directory("downloads", base, create=True)
        self.assertTrue(relative["valid"], relative)
        self.assertEqual(Path(relative["path"]), (base / "downloads").resolve())

    def test_provider_attempts_exclude_explicitly_retried_source(self):
        temp = self.artifacts / "fallback"
        temp.mkdir(parents=True, exist_ok=True)
        store = TaskStore(temp / "state.sqlite3")
        task = DownloadTask("https://source.test/a", str(temp))
        store.save(task)
        fallback = ProviderFallback(store)
        alternatives = [{"url": "https://alt.test/a", "provider_id": "generic"},
                        {"url": "https://other.test/a", "provider_id": "gofile"}]
        first = fallback.candidates(task.id, alternatives)
        self.assertEqual(len(first), 2)
        fallback.record(task.id, first[0]["url"], "generic", "direct", "failed", 429, error="rate limited")
        self.assertEqual([item["provider_id"] for item in fallback.candidates(task.id, alternatives)], ["gofile"])
        store.close()

    def test_route_policy_and_cooldown(self):
        self.assertTrue(route_error_is_switchable("connection reset by peer"))
        self.assertFalse(route_error_is_switchable("quota exceeded", 429, "rate_limit"))
        temp = self.artifacts / "routes"
        temp.mkdir(parents=True, exist_ok=True)
        store = TaskStore(temp / "state.sqlite3")
        manager = RouteManager(store)
        manager.save({"id": "express", "kind": "docker_socks5", "endpoint": "socks5://127.0.0.1:1080", "region": "Canada"})
        task = DownloadTask("https://source.test/a", str(temp))
        store.save(task)
        manager.record_attempt(task.id, "direct", "failed", "timeout")
        manager.record_attempt(task.id, "express", "failed", "timeout")
        self.assertIsNone(manager.next_profile(task.id, ["direct", "express"]))
        self.assertEqual(manager.next_profile(task.id, ["express", "new"]), "new")
        store.close()

    def test_engine_persists_download_and_archive_locations(self):
        root = self.artifacts / "settings-service"
        service = EngineService(root)
        destination = root / "drive-a" / "downloads"
        archive = root / "drive-b" / "archives"
        try:
            self.assertTrue(service.dispatch("set_download_directory", {"directory": str(destination)})["valid"])
            self.assertTrue(service.dispatch("set_archive_directory", {"directory": str(archive)})["valid"])
        finally:
            service.close()
        service = EngineService(root)
        try:
            self.assertEqual(Path(service.dispatch("get_download_directory", {})["path"]), destination.resolve())
            self.assertEqual(Path(service.dispatch("get_archive_directory", {})["path"]), archive.resolve())
        finally:
            service.close()

    def test_task_alternate_urls_survive_restart(self):
        root = self.artifacts / "alternate-service"
        service = EngineService(root)
        try:
            task = service.dispatch("add_task", {"url": "https://source.test/a", "alternatives": [
                {"url": "https://mirror.test/a", "provider_id": "generic"}]})
            self.assertEqual(service.dispatch("select_alternate_source", {"task_id": task["id"]})[0]["provider_id"], "generic")
        finally:
            service.close()
        service = EngineService(root)
        try:
            loaded = service.store.get(task["id"])
            self.assertEqual(loaded.alternate_urls[0]["url"], "https://mirror.test/a")
        finally:
            service.close()

    def test_extract_task_uses_configured_archive_directory(self):
        root = self.artifacts / "extract-service"
        service = EngineService(root)
        try:
            task = service.dispatch("add_task", {"url": "https://example.test/archive.zip"})
            loaded = service.store.get(task["id"])
            archive = Path(loaded.destination) / "archive.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("hello.txt", "hello")
            loaded.resolved = [ResolvedItem("generic", loaded.source_url, "archive.zip",
                                            size=archive.stat().st_size, direct_url=loaded.source_url)]
            service.store.save(loaded)
            result = service.dispatch("extract_task", {"task_id": loaded.id})
            self.assertEqual(len(result["archives"]), 1)
            self.assertTrue(Path(result["archives"][0]["output_directory"]).is_dir())
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
