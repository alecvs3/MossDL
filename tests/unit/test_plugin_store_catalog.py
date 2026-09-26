"""Settings -> Plugins shows every bundled plugin by a human name, in a category."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.plugin_catalog import PLUGIN_CATEGORIES, ManifestError, validate_manifest  # noqa: E402
from engine.plugins import PluginRegistry  # noqa: E402


class PluginStoreCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = PluginRegistry().provider_catalog()

    def test_every_plugin_has_a_category(self):
        missing = [p["id"] for p in self.catalog if p.get("category") not in PLUGIN_CATEGORIES]
        self.assertEqual(missing, [])

    def test_names_are_written_for_people(self):
        # Joined identifiers ("ArchiveOrg", "CloudMailRu") or bare ids ("mega") read as bugs in a store page.
        joined = {"ArchiveOrg", "CloudMailRu", "CheveretoGeneric", "WordPressMedia", "YandexDisk", "generic", "mega"}
        self.assertFalse(joined & {p["display_name"] for p in self.catalog})
        names = {p["id"]: p["display_name"] for p in self.catalog}
        self.assertEqual(names["archive_org"], "Internet Archive")
        self.assertEqual(names["mega"], "MEGA")
        self.assertEqual(names["transfer.it"], "Transfer.it")

    def test_the_catalog_keeps_its_old_fields(self):
        # Provider contracts are append-only.
        for key in ("id", "display_name", "icon", "hosts", "enabled"):
            self.assertIn(key, self.catalog[0])

    def test_an_unknown_category_is_refused(self):
        manifest = {"schema_version": 2, "id": "x", "version": "1", "hosts": ["x.test"], "match": {},
                    "capabilities": {}, "limits": {}, "hooks": {"resolve": "resolve"}, "category": "games"}
        with self.assertRaises(ManifestError):
            validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
