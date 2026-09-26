import json
import tempfile
import time
import unittest
from pathlib import Path

from engine.download_logger import (
    DownloadLoggerRegistry,
    DownloadSessionLogger,
    _redact_secrets,
    _sanitize_log_filename,
)


class TestDownloadLogger(unittest.TestCase):
    def test_sanitize_filename(self) -> None:
        raw = 'My:Illegal*Folder/Name?"test<file>|part.zip'
        cleaned = _sanitize_log_filename(raw)
        self.assertNotIn(":", cleaned)
        self.assertNotIn("*", cleaned)
        self.assertNotIn("/", cleaned)
        self.assertNotIn("?", cleaned)
        self.assertNotIn("<", cleaned)
        self.assertNotIn(">", cleaned)
        self.assertNotIn("|", cleaned)

    def test_redact_secrets(self) -> None:
        text = "https://example.com/file?token=my_secret_token_123&foo=bar"
        redacted = _redact_secrets(text)
        self.assertNotIn("my_secret_token_123", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_session_logger_telemetry_and_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = DownloadSessionLogger(
                task_id="task-test-1234",
                display_name="large_benchmark_file.iso",
                source_url="https://drive.google.com/uc?id=123",
                provider="gdrive",
                destination_path=str(Path(temp_dir) / "downloads"),
                total_size=100 * 1024 * 1024,  # 100 MB
                backend="custom",
                streams_count=8,
                logs_dir=temp_dir,
            )

            # Record some events
            logger.log_event("CONNECT", "Connected to remote server", {"ip": "142.250.190.46"})

            # Simulate 5 seconds of downloading
            fake_time = logger.started_at
            bytes_transferred = 0

            # Step 1: Normal speed 20 MB/s
            logger._last_second_tick = fake_time
            bytes_transferred += 20 * 1024 * 1024
            logger.record_progress(bytes_transferred, current_speed_bps=20 * 1024 * 1024, average_speed_bps=20 * 1024 * 1024)

            # Step 2: 25 MB/s
            logger._last_second_tick = fake_time - 1.0
            bytes_transferred += 25 * 1024 * 1024
            logger.record_progress(bytes_transferred, current_speed_bps=25 * 1024 * 1024, average_speed_bps=22.5 * 1024 * 1024)

            # Step 3: Stall / drop
            logger._last_second_tick = fake_time - 2.0
            logger._stall_start_time = fake_time - 1.0
            logger._stall_start_bytes = bytes_transferred
            # Stalled sample (< 50 KB)
            bytes_transferred += 10 * 1024
            logger.record_progress(bytes_transferred, current_speed_bps=10 * 1024, average_speed_bps=15 * 1024 * 1024)

            # Finalize
            out = logger.finalize(status="completed", integrity_state="verified")
            self.assertIsNotNone(out)
            self.assertTrue(Path(out["json_log"]).exists())
            self.assertTrue(Path(out["readable_log"]).exists())

            # Verify JSON content
            with open(out["json_log"], "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["schema_version"], "1.0.0")
            self.assertEqual(data["summary"]["task_id"], "task-test-1234")
            self.assertEqual(data["summary"]["provider"], "gdrive")
            self.assertEqual(data["summary"]["status"], "completed")
            self.assertIn("median_speed_mb_s", data["summary"])
            self.assertIn("p95_speed_mb_s", data["summary"])
            self.assertIn("stability_index_cv", data["summary"])
            self.assertIn("stability_rating", data["summary"])

            # Verify human-readable report content
            with open(out["readable_log"], "r", encoding="utf-8") as f:
                text = f.read()
            self.assertIn("NEXLOAD VERBOSE DOWNLOAD BENCHMARK & DIAGNOSTIC REPORT", text)
            self.assertIn("SECOND-BY-SECOND SPEED TIMELINE (ASCII Graph)", text)
            self.assertIn("File Name:", text)
            self.assertIn("KEY PERFORMANCE & STABILITY METRICS (Direct vs JDownloader Comparison)", text)

    def test_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reg = DownloadLoggerRegistry()
            logger = reg.create(
                task_id="task-reg-1",
                display_name="test.zip",
                source_url="https://example.com/test.zip",
                provider="direct",
                destination_path=temp_dir,
                total_size=1024,
            )
            self.assertIs(reg.get("task-reg-1"), logger)
            out = reg.finalize("task-reg-1", status="completed")
            self.assertIsNotNone(out)
            self.assertIsNone(reg.get("task-reg-1"))

    def test_unknown_length_report_backfills_total_and_reaches_100_percent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = DownloadSessionLogger(
                task_id="unknown-length",
                display_name="stream.bin",
                source_url="https://example.com/stream.bin",
                provider="direct",
                destination_path=temp_dir,
                total_size=0,
                logs_dir=temp_dir,
            )
            logger.set_total_size(50 * 1024 * 1024)
            logger.record_progress(50 * 1024 * 1024, eta_seconds=0.0, force=True)
            out = logger.finalize()
            data = json.loads(Path(out["json_log"]).read_text(encoding="utf-8"))
            self.assertEqual(data["summary"]["total_size_bytes"], 50 * 1024 * 1024)
            self.assertEqual(data["second_by_second_telemetry"][-1]["percent_complete"], 100.0)


if __name__ == "__main__":
    unittest.main()
