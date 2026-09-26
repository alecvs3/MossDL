"""Filter-list blocking in the crawl and capture review, compiled by the core."""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.adblock import FULL_LISTS, AdBlocker  # noqa: E402
from engine.capture import with_file_kinds  # noqa: E402
from engine.dom_cleaner import DomCleaner  # noqa: E402
from engine.rust_session import find_transfer_core  # noqa: E402

PAGE = """<html><body>
<a href="https://mega.nz/file/abc#key">Download from MEGA</a>
<a href="https://clicks.adhost.test/go?id=7">Download Now (Fast)</a>
<div class="zq-slot-7"><a href="https://files.example/other">Get it</a></div>
<a class="zq-slot-7" href="https://files.example/promo">Mirror 2</a>
</body></html>"""


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@unittest.skipIf(find_transfer_core() is None, "transfer-core binary is not built")
class AdBlockTests(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.blocker = AdBlocker(Path(self._dir.name))
        self.blocker.dir.mkdir(parents=True)
        (self.blocker.dir / "easylist.txt").write_text("||adhost.test^\n##.zq-slot-7\n", encoding="utf-8")

    def test_the_crawl_drops_what_the_lists_block(self):
        cleaner = DomCleaner(base_url="https://site.test/post")
        cleaner.feed(PAGE)
        stripped = self.blocker.strip_page(cleaner, "https://site.test/post")
        kept = [e.target_url for e in cleaner.elements]
        self.assertIn("https://mega.nz/file/abc#key", kept)
        self.assertNotIn("https://clicks.adhost.test/go?id=7", kept)
        self.assertNotIn("https://files.example/promo", kept, "a cosmetic rule hides elements by class")
        reasons = {e.target_url: e.filter_reason for e in cleaner.stripped_ads}
        self.assertEqual(reasons["https://clicks.adhost.test/go?id=7"], "filter_list")
        self.assertEqual(reasons["https://files.example/promo"], "filter_list_cosmetic")
        self.assertGreaterEqual(stripped, 2)

    def test_capture_review_marks_blocked_requests_as_noise(self):
        urls = ["https://clicks.adhost.test/pixel.gif", "https://cdn.files.example/game.zip"]
        blocked = self.blocker.blocked_urls(urls)
        batch = with_file_kinds({"candidates": [{"url": u} for u in urls]}, blocked)
        self.assertEqual(batch["candidates"][0]["noise"], "ad or tracker (filter lists)")
        self.assertIsNone(batch["candidates"][1]["noise"])

    def test_without_downloaded_lists_the_builtin_domains_are_used(self):
        (self.blocker.dir / "easylist.txt").unlink()
        self.assertEqual(self.blocker.lists(), [self.blocker.dir / "builtin.txt"])
        self.assertTrue(self.blocker.blocked_urls(["https://ad.doubleclick.net/x"]))

    def test_update_keeps_the_old_copy_of_a_list_that_fails(self):
        def fake(request, timeout):
            url = request if isinstance(request, str) else request.full_url
            if url == FULL_LISTS["easylist"]:
                raise OSError("network down")
            return _Response(b"! list\n||tracker.test^\n")
        with patch("engine.route_http.urlopen", fake):
            results = self.blocker.update()
        self.assertFalse(results["easylist"]["ok"])
        self.assertTrue(results["easyprivacy"]["ok"])
        self.assertIn("||adhost.test^", (self.blocker.dir / "easylist.txt").read_text(encoding="utf-8"))
        self.assertTrue(self.blocker.blocked_urls(["https://tracker.test/p.js"]), "new lists are compiled on next use")

    def test_list_updates_identify_themselves(self):
        # easylist.to answers Python's default agent with 403.
        seen = []

        def fake(request, timeout):
            seen.append(request.get_header("User-agent"))
            return _Response(b"! list\n||tracker.test^\n")
        with patch("engine.route_http.urlopen", fake):
            self.blocker.update()
        self.assertTrue(seen and all(agent and agent.startswith("MossDL/") for agent in seen), seen)

    def test_an_unavailable_engine_is_reported_not_hidden(self):
        with patch("engine.rust_session.CoreSession.request", side_effect=RuntimeError("core gone")), \
                patch("engine.adblock._record") as record:
            self.assertIsNone(self.blocker.check([{"url": "https://clicks.adhost.test/"}]))
        self.assertIn("ADBLOCK_UNAVAILABLE", record.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
