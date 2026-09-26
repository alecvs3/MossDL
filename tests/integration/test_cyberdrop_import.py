from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import shutil
import unittest
from pathlib import Path

from engine.cyberdrop_import import build_specs, check_active_plugins, generate_staging, test_staging


ROOT = _ROOT


# Compares against a local checkout of cyberdrop-dl (git-ignored, not redistributed).
@unittest.skipUnless((ROOT / "clone_reference" / "cyberdrop-dl").is_dir(), "clone_reference/cyberdrop-dl is not checked out")
class CyberdropImportTests(unittest.TestCase):
    def test_inventory_is_static_and_covers_the_reference_overlap(self):
        specs = build_specs(ROOT / "plugins", ROOT / "clone_reference" / "cyberdrop-dl")
        self.assertGreaterEqual(len(specs), 70)
        by_id = {spec.provider_id: spec for spec in specs}
        self.assertIn("mediafire", by_id)
        self.assertIn("mega", by_id)
        self.assertTrue(by_id["mediafire"].fixture_cases)
        self.assertTrue(by_id["mega"].fixture_cases)
        self.assertEqual(by_id["mediafire"].domain, "mediafire")

    def test_fixture_normalization_drops_secret_query_keys(self):
        specs = build_specs(ROOT / "plugins", ROOT / "clone_reference" / "cyberdrop-dl")
        serialized = json.dumps([spec.to_dict() for spec in specs], sort_keys=True)
        self.assertNotIn("access_token=", serialized)
        self.assertNotIn("x-amz-signature=", serialized)
        self.assertNotIn("Ijoqfoqzesat1LDq5NKc-Q", serialized)

    def test_generation_is_repeatable_and_staging_contracts_pass(self):
        staging = ROOT / ".cyberdrop-import-test-output"
        if staging.exists():
            shutil.rmtree(staging)
        try:
            first = generate_staging(ROOT / "plugins", ROOT / "clone_reference" / "cyberdrop-dl", staging)
            first_bytes = (staging / "migration-report.json").read_bytes()
            second = generate_staging(ROOT / "plugins", ROOT / "clone_reference" / "cyberdrop-dl", staging)
            self.assertEqual(first["summary"], second["summary"])
            self.assertEqual(first_bytes, (staging / "migration-report.json").read_bytes())
            self.assertEqual(first["summary"]["providers"], 72)
            self.assertEqual(first["summary"]["local_only_audits"], 1)
            self.assertTrue((staging / "mediafire" / "fixtures" / "reference.json").is_file())
            tested = test_staging(staging)
            self.assertEqual(tested["summary"]["failed"], 0)
            self.assertGreaterEqual(tested["summary"]["passed"], 70)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def test_offline_active_plugin_health_matrix(self):
        report = check_active_plugins(
            ROOT / "plugins", ROOT / "clone_reference" / "cyberdrop-dl", live=False
        )
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertGreaterEqual(report["summary"]["providers"], 72)
        self.assertIn("mediafire", {item["provider_id"] for item in report["providers"]})
        self.assertIn("mega", {item["provider_id"] for item in report["providers"]})
        self.assertGreaterEqual(report["summary"]["har_fixtures"], 1)


if __name__ == "__main__":
    unittest.main()
