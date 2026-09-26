import json
import tempfile
import unittest
from pathlib import Path

from engine.db import TaskStore
from engine.download_logger import DownloadSessionLogger
from engine.models import DownloadTask


class LifecycleReliabilityTests(unittest.TestCase):
    def test_package_manifest_fields_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "downloads.sqlite3"
            store = TaskStore(path)
            task = DownloadTask(
                "https://fixture.local/Game.part01.rar", temp,
                display_name="Game.part01.rar", id="leader",
                package_key="fixture.local:game", package_part_number=1,
                package_part_count=4, package_leader_id="leader",
            )
            store.save(task)
            store.close()

            reopened = TaskStore(path)
            loaded = reopened.get("leader")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.package_key, "fixture.local:game")
            self.assertEqual(loaded.package_part_number, 1)
            self.assertEqual(loaded.package_part_count, 4)
            self.assertEqual(loaded.package_leader_id, "leader")
            reopened.close()

    def test_nonterminal_full_byte_sample_is_not_100_percent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = DownloadSessionLogger(
                task_id="progress-task", display_name="stream.bin",
                source_url="https://fixture.local/stream.bin", provider="fixture",
                destination_path=temp, total_size=100,
                logs_dir=temp,
            )
            logger._last_second_tick -= 1.0
            logger.record_progress(100, force=False)
            logger.record_progress(100, eta_seconds=0.0, force=True)
            report = logger.finalize(status="completed", integrity_state="verified")
            data = json.loads(Path(report["json_log"]).read_text(encoding="utf-8"))
            self.assertEqual(data["second_by_second_telemetry"][0]["percent_complete"], 99.0)
            self.assertEqual(data["second_by_second_telemetry"][-1]["percent_complete"], 100.0)
            self.assertEqual(data["second_by_second_telemetry"][-1]["status_flag"], "COMPLETED")
            self.assertFalse(data["second_by_second_telemetry"][-1]["is_stall"])

    def test_verification_phase_is_not_recorded_as_socket_stall(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = DownloadSessionLogger(
                task_id="verify-phase-task", display_name="verify.bin",
                source_url="https://fixture.local/verify.bin", provider="fixture",
                destination_path=temp, total_size=100,
                logs_dir=temp,
            )
            logger._last_second_tick -= 1.0
            logger.record_progress(100, force=False)
            logger.set_phase("verifying")
            logger._last_second_tick -= 1.0
            logger.record_progress(100, force=False)
            report = logger.finalize(status="completed", integrity_state="verified")
            data = json.loads(Path(report["json_log"]).read_text(encoding="utf-8"))
            verifying = data["second_by_second_telemetry"][-1]
            self.assertEqual(verifying["phase"], "verifying")
            self.assertEqual(verifying["status_flag"], "VERIFYING")
            self.assertFalse(verifying["is_stall"])


if __name__ == "__main__":
    unittest.main()
