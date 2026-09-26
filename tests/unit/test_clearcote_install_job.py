"""The Clearcote install runs in the background and reports its real progress."""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.clearcote_manager import InstallJob  # noqa: E402


def _wait(job: InstallJob) -> dict:
    deadline = time.monotonic() + 5
    while job.snapshot()["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    return job.snapshot()


class InstallJobTests(unittest.TestCase):
    def test_progress_is_what_the_installer_reports(self):
        gate, seen = threading.Event(), []

        def fake_install(progress_callback):
            progress_callback(42, "Downloading: 97.0 / 230.0 MB")
            seen.append(True)
            gate.wait(2)
            return {"installed": True, "size_mb": 446}

        job = InstallJob()
        with patch("engine.clearcote_manager.install_clearcote", fake_install):
            job.start()
            while not seen:
                time.sleep(0.01)
            mid = job.snapshot()
            self.assertEqual((mid["running"], mid["percent"]), (True, 42))
            self.assertTrue(job.start()["running"], "a second start joins the running install")
            gate.set()
            done = _wait(job)
        self.assertEqual((done["running"], done["percent"], done["status"]["installed"]), (False, 100, True))

    def test_a_failure_is_reported(self):
        def broken(progress_callback):
            raise RuntimeError("does not match the checksum")
        job = InstallJob()
        with patch("engine.clearcote_manager.install_clearcote", broken):
            job.start()
            done = _wait(job)
        self.assertFalse(done["running"])
        self.assertIn("checksum", done["error"])


if __name__ == "__main__":
    unittest.main()
