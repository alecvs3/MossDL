from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import critical_trace
from engine.browser_solver import BrowserSolverDaemon
from engine.trace_analysis import analyze_trace


class CriticalTraceTests(unittest.TestCase):
    def tearDown(self) -> None:
        critical_trace.set_external_sink(None)
        if critical_trace.active():
            critical_trace.stop()

    def test_source_timestamps_identity_spans_and_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            started = critical_trace.start(temp, run_id="run-1", attributes={"profile": "fixture"})
            with critical_trace.bind(
                package_id="pkg", task_id="task-1", part_number=2, attempt_id=3,
            ):
                with critical_trace.span("resolve.provider", resource="provider", resource_id="example.test"):
                    critical_trace.mark(
                        "provider.wait.waiting", event="state",
                        token="must-not-appear", url="https://example.test/file?token=secret",
                    )
            stopped = critical_trace.stop()

            self.assertEqual(started["trace_path"], stopped["trace_path"])
            self.assertTrue(stopped["valid"])
            rows = [json.loads(line) for line in Path(stopped["trace_path"]).read_text().splitlines()]
            provider_rows = [row for row in rows if row["phase"] == "resolve.provider"]
            self.assertEqual(["span_start", "span_end"], [row["event"] for row in provider_rows])
            self.assertEqual("pkg", provider_rows[0]["package_id"])
            self.assertEqual("task-1", provider_rows[0]["task_id"])
            self.assertEqual(2, provider_rows[0]["part_number"])
            self.assertIsInstance(provider_rows[0]["t_mono_ns"], int)
            self.assertGreaterEqual(provider_rows[1]["t_mono_ns"], provider_rows[0]["t_mono_ns"])
            wait = next(row for row in rows if row["phase"] == "provider.wait.waiting")
            self.assertEqual("[REDACTED]", wait["attributes"]["token"])
            self.assertNotIn("secret", wait["attributes"]["url"])

            report = analyze_trace(stopped["trace_path"])
            self.assertTrue(report["valid"])
            duration = next(item for item in report["phase_durations"]
                            if item["phase"] == "resolve.provider")
            self.assertEqual(1, duration["count"])

    def test_nested_spans_keep_parent_relationship(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            critical_trace.start(temp, run_id="nested")
            with critical_trace.span("outer") as outer:
                with critical_trace.span("inner"):
                    pass
            stopped = critical_trace.stop()
            rows = [json.loads(line) for line in Path(stopped["trace_path"]).read_text().splitlines()]
            inner = next(row for row in rows
                         if row["phase"] == "inner" and row["event"] == "span_start")
            self.assertEqual(outer, inner["parent_span_id"])

    def test_browser_actor_preserves_trace_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            critical_trace.start(temp, run_id="actor")
            daemon = BrowserSolverDaemon(max_lanes=1)

            def fake_execute(*_args, **_kwargs):
                critical_trace.mark("fixture.actor", event="state")
                time.sleep(0.01)
                return {"success": True, "engine": "fixture"}

            try:
                with patch.object(daemon, "_execute_solve_on_actor", side_effect=fake_execute):
                    with critical_trace.bind(package_id="pkg", task_id="task-actor", part_number=3):
                        result = daemon.solve_challenge_sync("https://example.test/file", timeout_seconds=1)
                self.assertTrue(result["success"])
            finally:
                daemon.close()
            stopped = critical_trace.stop()
            rows = [json.loads(line) for line in Path(stopped["trace_path"]).read_text().splitlines()]
            actor = next(row for row in rows if row["phase"] == "fixture.actor")
            self.assertEqual("pkg", actor["package_id"])
            self.assertEqual("task-actor", actor["task_id"])
            self.assertEqual(3, actor["part_number"])

    def test_process_bridge_preserves_source_timestamp_and_identity(self) -> None:
        bridged = []
        critical_trace.set_external_sink(bridged.append)
        with critical_trace.bind(package_id="pkg-worker", task_id="task-worker", part_number=4):
            critical_trace.mark("provider.fixture.request", status=200)
        critical_trace.set_external_sink(None)
        self.assertEqual(1, len(bridged))

        with tempfile.TemporaryDirectory() as temp:
            critical_trace.start(temp, run_id="parent")
            self.assertTrue(critical_trace.ingest_external(bridged[0]))
            stopped = critical_trace.stop()
            rows = [json.loads(line) for line in Path(stopped["trace_path"]).read_text().splitlines()]
            worker = next(row for row in rows if row["phase"] == "provider.fixture.request")
            self.assertEqual(bridged[0]["t_mono_ns"], worker["t_mono_ns"])
            self.assertEqual("pkg-worker", worker["package_id"])
            self.assertEqual("task-worker", worker["task_id"])


if __name__ == "__main__":
    unittest.main()
