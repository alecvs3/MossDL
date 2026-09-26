import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine.benchmarking import BenchmarkReport, BenchmarkRunner
from engine.errors import ProviderMappedError
from engine.observability import (
    OperationalAggregator,
    classify_error_outcome,
    human_operational_summary,
    redact_operational,
)
from engine.reliability import FailureClass
from engine.service import EngineService


class OperationalObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = EngineService(self.temp_dir.name)

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def test_report_schema_and_defaults(self):
        aggregator = OperationalAggregator(self.service)
        report = aggregator.generate_report()
        data = report.to_dict()

        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["report_version"], "operational-report/1")
        self.assertEqual(data["source"], "engine")
        self.assertIn("metrics", data)

        metrics = data["metrics"]
        required_metrics = {
            "throughput_bytes_per_second",
            "observed_concurrency",
            "peak_concurrency",
            "queue_wait_seconds",
            "retry_count",
            "refresh_count",
            "disk_read_bytes",
            "disk_write_bytes",
            "disk_write_seconds",
        }
        for rm in required_metrics:
            self.assertIn(rm, metrics)

    def test_redact_operational_strips_sensitive_information(self):
        sensitive_payload = {
            "token": "secret-token-12345",
            "authorization": "Bearer secret-bearer-abcde",
            "cookie": "session=secret-cookie-val",
            "url": "https://cdn.example.test/file.zip?signature=secret-sig&token=secret-url-token",
            "nested": {
                "credential_ref": "keychain://secret/account",
                "safe_field": "public_data",
                "message": "Error contacting https://api.example.test/token?secret=hidden with key=123",
            },
        }
        redacted = redact_operational(sensitive_payload)
        serialized = json.dumps(redacted)

        self.assertNotIn("secret-token-12345", serialized)
        self.assertNotIn("secret-bearer-abcde", serialized)
        self.assertNotIn("secret-cookie-val", serialized)
        self.assertNotIn("secret-sig", serialized)
        self.assertNotIn("secret-url-token", serialized)
        self.assertNotIn("https://api.example.test/token?secret=hidden", serialized)
        self.assertEqual(redacted["nested"]["safe_field"], "public_data")

    def test_correlates_benchmark_and_populates_metrics(self):
        with tempfile.TemporaryDirectory() as bm_temp:
            runner = BenchmarkRunner(Path(bm_temp), payload_size=128 * 1024, advertised_rate_bytes_per_second=128 * 1024)
            bm_report = runner.run("single-stream")

        aggregator = OperationalAggregator(self.service)
        report = aggregator.generate_report(benchmark_report=bm_report)
        data = report.to_dict()

        self.assertIsNotNone(data["benchmark"])
        self.assertGreater(len(data["benchmark"]["results"]), 0)
        self.assertGreater(data["metrics"]["throughput_bytes_per_second"], 0.0)

    def test_task_diagnostics_and_failure_classification(self):
        task = self.service.dispatch("add_task", {
            "url": "https://example.test/data.bin",
            "destination": self.temp_dir.name,
        })
        task_id = task["id"]

        # Record error and diagnostics
        self.service.store.record_diagnostic(task_id, "ERROR", "download", "HTTP 429 Too Many Requests received")
        task_obj = self.service.store.get(task_id)
        task_obj.state = "failed"
        task_obj.error = "HTTP 429 Too Many Requests: Rate Limited"
        self.service.store.save(task_obj)

        aggregator = OperationalAggregator(self.service)
        report = aggregator.generate_report(task_id=task_id)
        data = report.to_dict()

        self.assertIsNotNone(data["failure_classification"])
        self.assertEqual(data["failure_classification"]["category"], FailureClass.RATE_LIMITED.value)
        self.assertEqual(data["failure_classification"]["state"], "failed")
        self.assertTrue(any("backoff" in r.lower() for r in data["recommendations"]))

    def test_service_dispatch_operational_report(self):
        result = self.service.dispatch("operational_report", {})
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["report_version"], "operational-report/1")
        self.assertIn("metrics", result)

    def test_human_summary_rendering(self):
        aggregator = OperationalAggregator(self.service)
        report = aggregator.generate_report()
        summary = human_operational_summary(report)

        self.assertIn("Operational & Observability Diagnostic Report", summary)
        self.assertIn("Throughput:", summary)
        self.assertIn("Concurrency", summary)
        self.assertIn("Disk I/O:", summary)

    def test_cli_operational_report_execution(self):
        script_path = Path(__file__).parents[1] / "scripts" / "operational_report.py"
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as f:
            out_json = Path(f.name)

        try:
            cmd = [sys.executable, str(script_path), "--output", str(out_json), "--format", "json"]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"CLI failed: {proc.stderr}")

            self.assertTrue(out_json.exists())
            content = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(content["schema_version"], 1)
            self.assertIn("metrics", content)
        finally:
            out_json.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()