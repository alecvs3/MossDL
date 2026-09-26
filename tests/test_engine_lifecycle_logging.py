import shutil
import tempfile
import unittest

from engine.service import EngineService
from engine.telemetry import telemetry_bus


class TestEngineLifecycleLogging(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        telemetry_bus.clear()
        self.service = EngineService(self.temp_dir)

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_boot_and_db_events(self):
        events = telemetry_bus.query()
        boot_events = [e for e in events if e["subsystem"] == "engine:boot"]
        db_events = [e for e in events if e["subsystem"] == "engine:db"]

        self.assertGreaterEqual(len(boot_events), 2)  # initializing + complete
        self.assertEqual(boot_events[0]["message"], "EngineService initializing")
        self.assertEqual(boot_events[1]["message"], "EngineService initialization complete")
        self.assertIn("plugins_count", boot_events[1]["context"])

        self.assertGreaterEqual(len(db_events), 1)
        self.assertEqual(db_events[0]["message"], "TaskStore database initialized and migrations applied")

    def test_rpc_success_telemetry(self):
        res = self.service.dispatch("api_info")
        self.assertIsNotNone(res)

        rpc_events = [e for e in telemetry_bus.query() if e["subsystem"] == "engine:rpc"]
        self.assertGreaterEqual(len(rpc_events), 1)
        event = rpc_events[-1]
        self.assertEqual(event["level"], "DEBUG")
        self.assertIn("RPC api_info completed", event["message"])
        self.assertIsNotNone(event["duration_ms"])
        self.assertEqual(event["context"]["method"], "api_info")

    def test_rpc_error_telemetry_captures_traceback(self):
        with self.assertRaises(KeyError):
            self.service.dispatch("nonexistent_method_xyz", {"auth_token": "secret_123"})

        error_events = [e for e in telemetry_bus.query() if e["subsystem"] == "engine:rpc" and e["level"] == "ERROR"]
        self.assertGreaterEqual(len(error_events), 1)
        err_event = error_events[-1]
        self.assertIn("RPC nonexistent_method_xyz failed", err_event["message"])
        self.assertEqual(err_event["error"]["type"], "KeyError")
        self.assertIn("Traceback (most recent call last):", err_event["error"]["stack"])
        # Ensure params were redacted
        self.assertEqual(err_event["context"]["params"]["auth_token"], "[REDACTED]")

    def test_settings_update_telemetry(self):
        current = self.service.dispatch("ui_settings_get")
        new_max = 6 if current["general"].get("maxConcurrent") != 6 else 7
        self.service.dispatch("ui_settings_update", {"settings": {"general": {"maxConcurrent": new_max}}})

        settings_events = [e for e in telemetry_bus.query() if e["subsystem"] == "settings:update"]
        self.assertGreaterEqual(len(settings_events), 1)
        last_setting_event = settings_events[-1]
        self.assertIn("general.maxConcurrent", last_setting_event["context"]["changed"])

    def test_teardown_telemetry(self):
        temp_d = tempfile.mkdtemp()
        s = EngineService(temp_d)
        telemetry_bus.clear()
        s.close()
        shutil.rmtree(temp_d, ignore_errors=True)

        teardown_events = [e for e in telemetry_bus.query() if e["subsystem"] == "engine:teardown"]
        self.assertEqual(len(teardown_events), 2)
        self.assertEqual(teardown_events[0]["message"], "EngineService teardown started")
        self.assertEqual(teardown_events[1]["message"], "EngineService teardown completed")


if __name__ == "__main__":
    unittest.main()
