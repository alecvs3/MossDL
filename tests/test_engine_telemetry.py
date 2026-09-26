import json
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.telemetry import (
    EngineTelemetryBus,
    LogEvent,
    TelemetryLoggingHandler,
    install_telemetry_hooks,
    redact_telemetry_data,
)


class TestEngineTelemetry(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = Path(self.temp_dir) / "test_engine.jsonl"
        self.bus = EngineTelemetryBus(capacity=100, log_file=self.log_file)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_record_and_monotonic_sequence(self):
        id1 = self.bus.record("INFO", "engine:core", "First event")
        id2 = self.bus.record("WARN", "engine:core", "Second event")
        id3 = self.bus.record("ERROR", "engine:rpc", "Third event")

        self.assertEqual(id1, 1)
        self.assertEqual(id2, 2)
        self.assertEqual(id3, 3)

        events = self.bus.query()
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["id"], 1)
        self.assertEqual(events[0]["message"], "First event")
        self.assertEqual(events[0]["level"], "INFO")
        self.assertEqual(events[2]["subsystem"], "engine:rpc")

    def test_deep_recursive_redaction(self):
        sensitive_data = {
            "token": "super_secret_token",
            "safe_key": "safe_value",
            "nested": {
                "password": "secret_password",
                "auth_headers": {"Authorization": "Bearer 12345", "User-Agent": "TransferManager"},
                "urls": ["https://example.com/download?token=xyz123&file=test.zip", "https://clean.org/test"],
            },
            "cookies": ["session_id=abcdef"],
        }

        redacted = redact_telemetry_data(sensitive_data)
        self.assertEqual(redacted["token"], "[REDACTED]")
        self.assertEqual(redacted["safe_key"], "safe_value")
        self.assertEqual(redacted["nested"]["password"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["auth_headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["auth_headers"]["User-Agent"], "TransferManager")
        self.assertEqual(redacted["cookies"], "[REDACTED]")
        self.assertIn("token=%5BREDACTED%5D", redacted["nested"]["urls"][0])
        self.assertEqual(redacted["nested"]["urls"][1], "https://clean.org/test")

    def test_ring_buffer_capacity_overflow(self):
        small_bus = EngineTelemetryBus(capacity=5)
        for i in range(10):
            small_bus.record("INFO", "engine:test", f"msg {i}")

        events = small_bus.query()
        self.assertEqual(len(events), 5)
        self.assertEqual(events[0]["id"], 6)
        self.assertEqual(events[4]["id"], 10)

    def test_query_filtering(self):
        self.bus.record("DEBUG", "engine:core", "debug msg")
        self.bus.record("INFO", "engine:core", "info msg")
        self.bus.record("WARN", "provider:mediafire", "warn msg")
        self.bus.record("ERROR", "provider:mega", "error msg")

        # Query since_id
        since_res = self.bus.query(since_id=2)
        self.assertEqual(len(since_res), 2)
        self.assertEqual(since_res[0]["id"], 3)

        # Query min_level
        warn_and_above = self.bus.query(min_level="WARN")
        self.assertEqual(len(warn_and_above), 2)
        self.assertEqual(warn_and_above[0]["level"], "WARN")
        self.assertEqual(warn_and_above[1]["level"], "ERROR")

        # Query subsystem with wildcard
        provider_events = self.bus.query(subsystem="provider:*")
        self.assertEqual(len(provider_events), 2)
        self.assertEqual(provider_events[0]["subsystem"], "provider:mediafire")
        self.assertEqual(provider_events[1]["subsystem"], "provider:mega")

    def test_disk_jsonl_output_and_clear(self):
        self.bus.record("INFO", "engine:boot", "booting up", {"pid": 12345})
        self.bus.record("ERROR", "engine:db", "db connection failed", error={"type": "OperationalError", "message": "locked"})

        self.assertTrue(self.log_file.exists())
        with open(self.log_file, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["message"], "booting up")
        self.assertEqual(lines[1]["error"]["type"], "OperationalError")

        # Clear in-memory buffer, file should remain intact
        self.bus.clear()
        self.assertEqual(len(self.bus.query()), 0)
        self.assertTrue(self.log_file.exists())

    def test_python_logging_handler_routing(self):
        handler = TelemetryLoggingHandler(self.bus)
        test_logger = logging.getLogger("test_subsystem_logger")
        test_logger.setLevel(logging.DEBUG)
        test_logger.addHandler(handler)

        test_logger.info("Logging from standard library logger")
        test_logger.error("Error from standard library logger")

        events = self.bus.query(subsystem="engine:test_subsystem_logger")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["level"], "INFO")
        self.assertEqual(events[0]["message"], "Logging from standard library logger")
        self.assertEqual(events[1]["level"], "ERROR")


if __name__ == "__main__":
    unittest.main()
