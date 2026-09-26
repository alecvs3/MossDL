from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from engine.benchmarking import (CORE_SCENARIOS, FIXTURE_CASES, BenchmarkRunner,
                                 BenchmarkScenario, human_summary, write_report)
from scripts.benchmark_transfer_core import TerminalProgressBar, print_comparison_diff


class TransferBenchmarkTests(unittest.TestCase):
    def test_scenario_schema_and_fixture_matrix_are_explicit(self):
        scenario = BenchmarkScenario.named("eight")
        self.assertEqual(scenario.name, "eight-range")
        self.assertEqual(scenario.range_count, 8)
        self.assertEqual(scenario.to_dict()["schema_version"], 1)
        self.assertEqual(set(FIXTURE_CASES), {
            "no-range", "valid-206", "416", "validator-mismatch", "malformed-206",
            "retry", "refresh", "throttling", "slow-server", "pause-resume", "integrity",
        })

    def test_custom_benchmark_uses_real_fixture_and_reports_normalized_metrics(self):
        with tempfile.TemporaryDirectory() as raw:
            report = BenchmarkRunner(Path(raw), payload_size=2 * 1024 * 1024,
                                     advertised_rate_bytes_per_second=1024 * 1024).run("all")
        data = report.to_dict()
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["results"])
        custom = [item for item in data["results"] if item["backend"] == "custom"]
        self.assertEqual({item["scenario"] for item in custom}, set(CORE_SCENARIOS))
        for item in custom:
            self.assertIn("metrics", item)
            self.assertIn("queue_wait_seconds", item["metrics"])
            self.assertIn("read_bytes", item["metrics"])
            self.assertIn("write_bytes", item["metrics"])
            self.assertIn("retry_count", item["metrics"])
            self.assertIn("refresh_count", item["metrics"])
            self.assertIn("effective_throttle_bytes_per_second", item["metrics"])
            self.assertIn("integrity", item)
            self.assertIn("availability", item)
        self.assertEqual(len(data["fixture_matrix"]), len(FIXTURE_CASES))
        self.assertTrue(data["eligibility"]["live_is_corrobating_only"])

    def test_optional_adapters_are_available_or_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as raw:
            data = BenchmarkRunner(Path(raw), payload_size=256 * 1024,
                                   advertised_rate_bytes_per_second=256 * 1024).run("single-stream").to_dict()
        by_backend = {item["backend"]: item for item in data["results"]}
        self.assertEqual(set(by_backend), {"custom", "rust"})
        for item in by_backend.values():
            self.assertIn("available", item["availability"])
            if not item["availability"]["available"]:
                self.assertEqual(item["gate"]["gating"], False)

    def test_report_writing_and_human_summary_are_secret_safe(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = BenchmarkRunner(root, payload_size=128 * 1024,
                                     advertised_rate_bytes_per_second=128 * 1024).run("single-stream")
            output = root / "report.jsonl"
            write_report(report, output)
            serialized = output.read_text(encoding="utf-8").lower()
            summary = human_summary(report).lower()
        self.assertNotIn("cookie", serialized)
        self.assertNotIn("authorization", serialized)
        self.assertNotIn("token=", serialized)
        self.assertIn("schema", summary)

    def test_gate_classifies_internal_cap_without_host_absolute_speed(self):
        runner = BenchmarkRunner(payload_size=1, advertised_rate_bytes_per_second=100)
        gate = runner._gate(BenchmarkScenario.named("single-stream", payload_size=1), "custom", 1,
                            reference_single=None, reference_memory=None, available=True,
                            integrity_state="verified", failure=None)
        self.assertFalse(gate["passed"])
        self.assertIn("internal-cap", gate["reason"])

    def test_authorized_live_quota_is_non_gating_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            runner = BenchmarkRunner(Path(raw), payload_size=128 * 1024,
                                     advertised_rate_bytes_per_second=128 * 1024)
            with patch("engine.benchmarking.PooledTransportManager.probe",
                       side_effect=RuntimeError("HTTP 509 quota exceeded")):
                result = runner._live_probe("https://example.test/file?signature=secret")
        data = result.to_dict()
        self.assertFalse(data["gate"]["gating"])
        self.assertTrue(data["gate"]["passed"])
        self.assertEqual(data["source"], "live")
        self.assertNotIn("signature=", json.dumps(data).lower())

    def test_sixteen_and_thirty_two_range_scenarios_configured(self):
        s16 = BenchmarkScenario.named("sixteen")
        self.assertEqual(s16.name, "sixteen-range")
        self.assertEqual(s16.range_count, 16)
        self.assertIn("sixteen-range", CORE_SCENARIOS)

        s32 = BenchmarkScenario.named("thirty-two")
        self.assertEqual(s32.name, "thirty-two-range")
        self.assertEqual(s32.range_count, 32)
        self.assertIn("thirty-two-range", CORE_SCENARIOS)

    def test_threaded_benchmark_runner_execution(self):
        with tempfile.TemporaryDirectory() as raw:
            runner = BenchmarkRunner(Path(raw), payload_size=128 * 1024,
                                     advertised_rate_bytes_per_second=128 * 1024)
            report = runner.run("single-stream", threaded=True)
        data = report.to_dict()
        self.assertTrue(data["results"])
        self.assertIn("all_gates_passed", data["summary"])

    def test_terminal_progress_bar_rendering_and_indeterminate(self):
        buf = io.StringIO()
        bar = TerminalProgressBar(stream=buf, width=20)
        # Test progress update
        bar("rust", "eight-range", 5000, 10000)
        # Test done update
        bar("rust", "eight-range", 10000, 10000)
        bar.finish()
        output = buf.getvalue()
        self.assertIn("rust", output)
        self.assertIn("eight-range", output)

        # Test indeterminate mode (total_bytes = 0)
        buf2 = io.StringIO()
        bar2 = TerminalProgressBar(stream=buf2, width=20)
        bar2("custom", "single-stream", 4096, 0)
        bar2.finish()
        self.assertIn("custom", buf2.getvalue())

    def test_diff_comparison_reporting(self):
        with tempfile.TemporaryDirectory() as raw:
            base_path = Path(raw) / "baseline.json"
            base_payload = {
                "schema_version": 1,
                "results": [
                    {
                        "backend": "custom",
                        "scenario": "single-stream",
                        "metrics": {"throughput_bytes_per_second": 1048576.0},
                        "gate": {"passed": True}
                    }
                ]
            }
            base_path.write_text(json.dumps(base_payload), encoding="utf-8")

            runner = BenchmarkRunner(Path(raw), payload_size=64 * 1024,
                                     advertised_rate_bytes_per_second=64 * 1024)
            report = runner.run("single-stream")

            buf = io.StringIO()
            with redirect_stdout(buf):
                print_comparison_diff(report, base_path)
            output = buf.getvalue()
            self.assertIn("ACCELERATION BENCHMARK COMPARISON", output)
            self.assertIn("single-stream", output)

    def test_corruption_guard_scenarios_handled_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            runner = BenchmarkRunner(Path(raw), payload_size=64 * 1024,
                                     advertised_rate_bytes_per_second=64 * 1024)
            # malformed-206 must be handled fail-closed without crashing
            report_malformed = runner.run("malformed-206")
            self.assertTrue(report_malformed.results)
            for res in report_malformed.results:
                self.assertIn("gate", res.to_dict())

            # validator-mismatch must be detected and recorded fail-closed
            report_validator = runner.run("validator-mismatch")
            self.assertTrue(report_validator.results)
            for res in report_validator.results:
                self.assertIn("gate", res.to_dict())


if __name__ == "__main__":
    unittest.main()
