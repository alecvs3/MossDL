import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.phase5_preflight import verify_phase5_contracts


class Phase5PreflightTests(unittest.TestCase):
    def test_preflight_passes_on_valid_contracts(self):
        passed, errors = verify_phase5_contracts()
        self.assertTrue(passed, f"Expected Phase 5 preflight to pass, errors: {errors}")
        self.assertEqual(len(errors), 0)

    def test_preflight_fails_closed_when_contract_missing(self):
        with patch("scripts.phase5_preflight.headless_required_scope", return_value=None):
            passed, errors = verify_phase5_contracts()
            self.assertFalse(passed)
            self.assertGreater(len(errors), 0)
            self.assertTrue(any("Missing required headless scope" in e for e in errors))

    def test_preflight_cli_exit_codes(self):
        script_path = Path(__file__).parents[1] / "scripts" / "phase5_preflight.py"
        res = subprocess.run([sys.executable, str(script_path), "--json"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Expected 0 exit, stderr: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertEqual(data["preflight"], "passed")
        self.assertEqual(data["phase"], 5)


if __name__ == "__main__":
    unittest.main()