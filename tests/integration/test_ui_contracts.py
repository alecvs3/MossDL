from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engine.service import EngineService


class UiContractTests(unittest.TestCase):
    def test_snapshot_and_settings_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            service = EngineService(root)
            try:
                snapshot = service.dispatch("ui_snapshot")
                self.assertIn("tasks", snapshot)
                self.assertIn("linkgrabber", snapshot)
                self.assertIn("settings", snapshot)
                self.assertIn("appearance", snapshot["settings"])
                self.assertIn("connectionsPerFile", snapshot["settings"]["network"])
                self.assertIn("colorMode", snapshot["settings"]["appearance"])
                self.assertIn("soundPreset", snapshot["settings"]["notifications"])

                result = service.dispatch("ui_settings_update", {"settings": {
                    "general": {"autoStart": False},
                    "appearance": {"rowSize": "small", "uiScale": "125%", "autoFullscreenScale": False},
                }})
                self.assertFalse(result["settings"]["general"]["autoStart"])
                self.assertEqual(result["settings"]["appearance"]["rowSize"], "small")
                self.assertEqual(result["settings"]["appearance"]["uiScale"], "125%")
                self.assertFalse(result["settings"]["appearance"]["autoFullscreenScale"])
                self.assertIn("general.autoStart", result["changed"])
                self.assertIn("appearance.uiScale", result["changed"])
                self.assertIn("appearance.autoFullscreenScale", result["changed"])

                numeric = service.dispatch("ui_settings_update", {"settings": {
                    "general": {"maxConcurrent": 4},
                    "network": {"connectionsPerFile": 8, "timeoutSeconds": 60},
                }})
                self.assertEqual(numeric["settings"]["general"]["maxConcurrent"], 4)
                self.assertEqual(numeric["settings"]["network"]["timeoutSeconds"], 60)
            finally:
                service.close()

            reopened = EngineService(root)
            try:
                settings = reopened.dispatch("ui_settings_get")
                self.assertFalse(settings["general"]["autoStart"])
                self.assertEqual(settings["appearance"]["rowSize"], "small")
            finally:
                reopened.close()

    def test_settings_reject_unknown_keys(self) -> None:
        with TemporaryDirectory() as directory:
            service = EngineService(Path(directory))
            try:
                with self.assertRaises(ValueError):
                    service.dispatch("ui_settings_update", {"settings": {"general": {"not_a_setting": True}}})
                with self.assertRaises(ValueError):
                    service.dispatch("ui_settings_update", {"settings": {"general": {"maxConcurrent": 0}}})
            finally:
                service.close()

    def test_runtime_settings_update_engine_policy_and_proxy(self) -> None:
        with TemporaryDirectory() as directory:
            service = EngineService(Path(directory))
            try:
                service.dispatch("ui_settings_update", {"settings": {
                    "general": {"maxConcurrent": 6, "retryFailed": False, "retryCount": 9},
                    "network": {"connectionsPerFile": 4, "timeoutSeconds": 45, "proxy": True, "proxyAddr": "127.0.0.1:8080"},
                }})
                self.assertEqual(service.resources.policy.max_active_tasks, 6)
                self.assertEqual(service.resources.policy.max_retries, 0)
                self.assertEqual(service.resources.policy.max_segments_per_file, 4)
                self.assertEqual(service.resources.policy.request_timeout_seconds, 45)
                proxy = next(item for item in service.routes.profiles() if item["id"] == "settings-proxy")
                self.assertEqual(proxy["kind"], "http_proxy")
                self.assertEqual(service.store.get_setting("active_route_profile"), "settings-proxy")
            finally:
                service.close()

    def test_snapshot_exposes_provider_presentation_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            service = EngineService(Path(directory))
            try:
                providers = service.dispatch("ui_snapshot")["providers"]
                self.assertTrue(providers)
                self.assertTrue(all({"id", "display_name", "hosts", "enabled"}.issubset(item) for item in providers))
            finally:
                service.close()

    def test_legacy_settings_are_migrated_to_current_sections(self) -> None:
        with TemporaryDirectory() as directory:
            service = EngineService(Path(directory))
            try:
                service.store.set_setting("ui_settings", {"network": {
                    "maxConcurrent": 7, "retryFailed": False, "retryCount": 5,
                }})
                settings = service.dispatch("ui_settings_get")
                self.assertEqual(settings["general"]["maxConcurrent"], 7)
                self.assertFalse(settings["general"]["retryFailed"])
                self.assertEqual(settings["general"]["retryCount"], 5)
                self.assertNotIn("maxConcurrent", settings["network"])
            finally:
                service.close()

    def test_linkgrabber_entries_are_live_snapshot_resources(self) -> None:
        with TemporaryDirectory() as directory:
            service = EngineService(Path(directory))
            try:
                created = service.dispatch("linkgrabber_add", {"text": "https://example.test/file.zip", "source": "manual"})
                self.assertEqual(len(created), 1)
                snapshot = service.dispatch("ui_snapshot")
                self.assertEqual(snapshot["linkgrabber"][0]["id"], created[0]["id"])
                updated = service.dispatch("linkgrabber_update", {"id": created[0]["id"], "selected": True})
                self.assertTrue(updated["selected"])
            finally:
                service.close()

    def test_delete_task_returns_consistent_action_result(self) -> None:
        with TemporaryDirectory() as directory:
            service = EngineService(Path(directory))
            try:
                task = service.dispatch("add_task", {"url": "https://example.test/file.zip", "destination": str(Path(directory) / "downloads")})
                result = service.dispatch("delete_task", {"id": task["id"]})
                self.assertEqual(result["affected_ids"], [task["id"]])
                self.assertEqual(result["skipped"], [])
                self.assertEqual(result["deleted"], task["id"])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
