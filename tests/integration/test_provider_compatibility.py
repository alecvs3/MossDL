from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import unittest
from pathlib import Path

from engine.plugins import PluginRegistry
from engine.provider_compatibility import (
    compatibility_report,
    compatibility_summary,
    compare_plugin_ecosystems,
)


ROOT = _ROOT
PLUGIN_ROOT = ROOT / "plugins"
CYBERDROP_ROOT = ROOT / "clone_reference" / "cyberdrop-dl"


# Compares against a local checkout of cyberdrop-dl (git-ignored, not redistributed).
@unittest.skipUnless((CYBERDROP_ROOT).is_dir(), "clone_reference/cyberdrop-dl is not checked out")
class ProviderCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = compare_plugin_ecosystems(PLUGIN_ROOT, CYBERDROP_ROOT)
        cls.overlaps = [record for record in cls.records if record.cyberdrop_overlap]

    def test_all_local_manifests_are_valid(self):
        errors = {record.provider_id: record.manifest_error for record in self.records if record.manifest_error}
        self.assertEqual(errors, {}, json.dumps(errors, indent=2))

    def test_every_overlap_has_local_resolve_and_reference_crawler(self):
        self.assertGreaterEqual(len(self.overlaps), 70)
        for record in self.overlaps:
            with self.subTest(provider=record.provider_id):
                self.assertIn("resolve", record.local_operations)
                self.assertTrue(record.cyberdrop_crawler)
                self.assertTrue(record.fixture_url)

    def test_fixture_urls_route_to_the_declared_local_plugin(self):
        registry = PluginRegistry()
        try:
            for record in self.overlaps:
                with self.subTest(provider=record.provider_id, url=record.routing_fixture_url):
                    self.assertEqual(registry.provider_for(record.routing_fixture_url), record.provider_id)
        finally:
            registry.close()

    def test_report_is_json_safe_and_has_explicit_fixture_counts(self):
        report = compatibility_report(PLUGIN_ROOT, CYBERDROP_ROOT)
        self.assertEqual(report["summary"], compatibility_summary(self.records))
        self.assertEqual(len(report["providers"]), len(self.records))
        self.assertEqual(json.loads(json.dumps(report))["summary"], report["summary"])

if __name__ == "__main__":
    unittest.main()
