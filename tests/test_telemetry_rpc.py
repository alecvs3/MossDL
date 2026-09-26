import json
import os
import shutil
import tempfile
import unittest

from engine.service import EngineService
from engine.telemetry import telemetry_bus
from engine.universal_resolver import UniversalResolver


class TestTelemetryRpc(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        telemetry_bus.clear()
        self.service = EngineService(self.temp_dir)

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_logs_query_pagination_and_filtering(self):
        telemetry_bus.clear()
        id1 = telemetry_bus.record("DEBUG", "test:sub", "debug event")
        id2 = telemetry_bus.record("INFO", "provider:test", "info event")
        id3 = telemetry_bus.record("WARN", "provider:mega", "warning event")
        id4 = telemetry_bus.record("ERROR", "test:sub", "error event")

        # Query all with subsystem filter
        res = self.service.dispatch("logs_query", {"subsystem": "test:sub", "limit": 10})
        self.assertEqual(res["count"], 2)

        # Query with since_id
        res_since = self.service.dispatch("logs_query", {"since_id": id2, "subsystem": "provider:*", "limit": 10})
        self.assertEqual(res_since["count"], 1)
        self.assertEqual(res_since["events"][0]["level"], "WARN")

        # Query with min_level
        res_level = self.service.dispatch("logs_query", {"min_level": "WARN", "subsystem": "test:sub"})
        self.assertEqual(res_level["count"], 1)
        self.assertEqual(res_level["events"][0]["level"], "ERROR")

        # Query with subsystem pattern
        res_sub = self.service.dispatch("logs_query", {"subsystem": "provider:*"})
        self.assertEqual(res_sub["count"], 2)
        self.assertEqual(res_sub["events"][0]["subsystem"], "provider:test")
        self.assertEqual(res_sub["events"][1]["subsystem"], "provider:mega")


    def test_logs_clear(self):
        telemetry_bus.record("INFO", "engine:core", "event to clear")
        self.assertGreater(len(telemetry_bus.query()), 0)

        clear_res = self.service.dispatch("logs_clear")
        self.assertTrue(clear_res.get("cleared"))

        # In-memory buffer is cleared, but subsequent RPC record will be captured
        query_res = self.service.dispatch("logs_query")
        # Only the logs_query RPC itself might be recorded
        for event in query_res["events"]:
            self.assertNotEqual(event["message"], "event to clear")

    def test_logs_export(self):
        telemetry_bus.record("INFO", "engine:test", "test export event", {"extra": "data"})
        export_file = os.path.join(self.temp_dir, "custom_export.json")

        res = self.service.dispatch("logs_export", {"target_path": export_file})
        self.assertEqual(res["export_path"], export_file)
        self.assertTrue(os.path.exists(export_file))

        with open(export_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("events", data)
        self.assertGreaterEqual(data["total_events"], 1)

    def test_universal_resolver_telemetry_and_redaction(self):
        # Mock fetcher returning HTML with sensitive embedded tokens
        mock_html = """
        <html>
            <head><title>Test Page</title></head>
            <body>
                <a href="https://files.example.com/download.zip?token=secret_abc123&session=user999">Download File</a>
            </body>
        </html>
        """
        resolver = UniversalResolver(fetcher=lambda url: ("text/html", mock_html))
        result = resolver.inspect("https://example.com/test-page?auth_token=super_secret")

        self.assertIn("candidates", result)

        events = telemetry_bus.query(subsystem="provider:universal")
        self.assertGreaterEqual(len(events), 1)

        # Check that none of the events leak the query auth_token
        for ev in events:
            ev_str = json.dumps(ev)
            self.assertNotIn("super_secret", ev_str)
            self.assertNotIn("secret_abc123", ev_str)

    def test_log_event_rpc_recording(self):
        telemetry_bus.clear()
        res = self.service.dispatch("log_event", {
            "level": "ERROR",
            "subsystem": "ui:render_crash",
            "message": "React root render failed",
            "context": {"component": "DownloadsTab", "user_id": "u-123"},
            "error": {"type": "TypeError", "message": "Cannot read properties of undefined", "traceback": "line 42 in foo"},
            "tier": "ui",
        })

        self.assertTrue(res.get("recorded"))
        self.assertIsInstance(res.get("seq_id"), int)

        events = telemetry_bus.query(subsystem="ui:render_crash")
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["level"], "ERROR")
        self.assertEqual(ev["tier"], "ui")
        self.assertEqual(ev["message"], "React root render failed")
        self.assertEqual(ev["context"]["component"], "DownloadsTab")
        self.assertEqual(ev["error"]["type"], "TypeError")


if __name__ == "__main__":
    unittest.main()
