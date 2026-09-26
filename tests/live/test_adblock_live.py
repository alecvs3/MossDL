"""Live: the real filter lists download, compile and block what they should.

Opt-in (network, ~4 MB): python scripts/check_all.py --live, or run this file.
Catches what unit tests cannot: a list host refusing us (the 403 easylist.to
sends to Python's default agent), a list that changed format, or a list update
that starts blocking the file hosts MossDL downloads from.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.adblock import FULL_LISTS, AdBlocker  # noqa: E402
from engine.rust_session import find_transfer_core  # noqa: E402

ADS = [
    ("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js", "script"),
    ("https://www.google-analytics.com/analytics.js", "script"),
    ("https://securepubads.g.doubleclick.net/tag/js/gpt.js", "script"),
    ("https://a.exoclick.com/tag_gen.js", "script"),
]
# Hosts whose files users download: a list update must never block these.
FILE_HOSTS = [
    "https://mega.nz/file/abc#key", "https://gofile.io/d/abc", "https://pixeldrain.com/u/abc",
    "https://github.com/owner/repo/releases/download/v1/app.zip", "https://drive.google.com/uc?id=abc",
    "https://www.mediafire.com/file/abc/file.zip", "https://buzzheavier.com/abc", "https://1fichier.com/?abc",
]


@unittest.skipIf(find_transfer_core() is None, "transfer-core binary is not built")
class LiveFilterLists(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = TemporaryDirectory()
        cls.blocker = AdBlocker(Path(cls._dir.name))
        cls.results = cls.blocker.update()

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def test_every_list_downloads_and_looks_like_a_filter_list(self):
        failed = {name: r for name, r in self.results.items() if not r.get("ok")}
        self.assertEqual(failed, {}, "list hosts must accept our requests")
        for name in FULL_LISTS:
            text = (self.blocker.dir / f"{name}.txt").read_text(encoding="utf-8", errors="replace")
            self.assertGreater(len(text), 20_000, name)
            self.assertTrue(text.lstrip().startswith(("[Adblock", "!")), f"{name} is not in filter-list format")

    def test_known_ads_are_blocked_and_file_hosts_are_not(self):
        ads = self.blocker.check([{"url": u, "source_url": "https://news.example/article", "type": t} for u, t in ADS])
        self.assertEqual(ads, [True] * len(ADS), dict(zip([u for u, _ in ADS], ads or [])))
        hosts = self.blocker.blocked_urls(FILE_HOSTS)
        self.assertEqual(hosts, set(), "a filter list now blocks a file host")

    def test_cosmetic_rules_hide_ad_slots(self):
        rules = self.blocker.cosmetic("https://news.example/article", ["adsbygoogle"], [])
        self.assertTrue(any("adsbygoogle" in s for s in rules["hide_selectors"]))


if __name__ == "__main__":
    unittest.main()
