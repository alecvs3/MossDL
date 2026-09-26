from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import shutil
import unittest
from pathlib import Path

from engine.db import TaskStore
from engine.linkgrabber import extract_urls, new_link


class AdditiveFeatureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ".test-artifacts" / "additive-features" / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = TaskStore(self.root / "test.sqlite3")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_linkgrabber_dedup_and_state(self):
        self.assertEqual(extract_urls("https://example.com/a https://example.com/a\nftp://host/file"),
                         ["https://example.com/a", "ftp://host/file"])
        first = self.store.save_link(new_link("https://example.com/a"))
        second = self.store.save_link(new_link("https://example.com/a"))
        self.assertEqual(first["id"], second["id"])
        self.store.update_link(first["id"], selected=True, state="selected")
        self.assertEqual(self.store.list_links()[0]["state"], "selected")

    def test_bandwidth_profile_round_trip(self):
        profile = self.store.save_bandwidth_profile({"id": "day", "name": "Day", "scope": "global",
                                                      "rate_bytes_per_second": 5 * 1024 * 1024,
                                                      "windows": [{"start_minute": 0, "end_minute": 60}]})
        self.assertEqual(profile["rate_bytes_per_second"], 5 * 1024 * 1024)
        self.assertEqual(self.store.list_bandwidth_profiles()[0]["windows"][0]["end_minute"], 60)

    def test_account_health_fields_are_additive(self):
        self.store.save_account({"id": "mega-1", "provider_id": "mega", "label": "MEGA",
                                 "credential_ref": "keychain://mega-1", "account_type": "oauth",
                                 "state": "healthy", "health": {"quota": "ok"}})
        account = self.store.list_accounts("mega")[0]
        self.assertEqual(account["account_type"], "oauth")
        self.assertEqual(account["health"]["quota"], "ok")


if __name__ == "__main__":
    unittest.main()
