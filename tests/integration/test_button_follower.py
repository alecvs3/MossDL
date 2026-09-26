"""The follower clicks through a file host the way a person would.

A local site stands in for one: a post with a fake ad "download" button and a
real mirror link, a mirror page with a "Free Download" button, a countdown
page whose link appears after a few seconds, and the file itself.
"""
from __future__ import annotations

import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine.adblock import AdBlocker  # noqa: E402
from engine.button_follower import is_file_response, pick  # noqa: E402
from engine.clearcote_manager import get_clearcote_executable  # noqa: E402
from engine.rust_session import find_transfer_core  # noqa: E402

PAGES = {
    "/post": """<html><body><h1>Some Game v1.2</h1>
        <a href="/">Home</a> <a href="/category/games">Games</a>
        <a class="big-green" href="https://ads.adhost.test/go?z=9"><b>DOWNLOAD NOW (Fast &amp; Secure)</b></a>
        <p>Mirrors:</p><a href="/mirror">Download from Mirror 1</a></body></html>""",
    "/mirror": """<html><body><div class="zq-slot-7"><a href="https://ads.adhost.test/x">Start Download</a></div>
        <p>game.zip · 1.2 GB</p><button id="free" onclick="location.href='/wait'">Free Download</button></body></html>""",
    "/wait": """<html><body><p id="msg">Please wait 3 seconds...</p>
        <a id="get" href="/file/game.zip" style="display:none">Download game.zip</a>
        <script>setTimeout(() => { document.getElementById('msg').textContent = 'Your link is ready';
          document.getElementById('get').style.display = 'inline'; }, 2500);</script></body></html>""",
}


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/file/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="game.zip"')
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"PK\x03\x04")
            return
        body = PAGES.get(self.path, "<html><body>nothing here</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class PickTests(unittest.TestCase):
    def test_file_responses(self):
        self.assertEqual(is_file_response({"content-disposition": 'attachment; filename="a b.zip"'}), "a b.zip")
        self.assertEqual(is_file_response({"content-type": "application/octet-stream"}), "")
        self.assertIsNone(is_file_response({"content-type": "text/html; charset=utf-8"}))

    def test_ads_hidden_and_navigation_are_passed_over_with_reasons(self):
        cands = [
            {"index": 0, "tag": "a", "text": "Home", "href": "https://site.test/", "id": "", "classes": [], "disabled": False, "visible": True, "area": 500},
            {"index": 1, "tag": "a", "text": "DOWNLOAD NOW", "href": "https://ads.test/go", "id": "", "classes": [], "disabled": False, "visible": True, "area": 9000},
            {"index": 2, "tag": "a", "text": "Download from Mirror 1", "href": "https://site.test/mirror", "id": "", "classes": [], "disabled": False, "visible": True, "area": 800},
            {"index": 3, "tag": "button", "text": "Download", "href": "", "id": "", "classes": [], "disabled": False, "visible": False, "area": 0},
        ]
        best, rejected = pick(cands, "https://site.test/post", set(), {"https://ads.test/go"})
        self.assertEqual(best["index"], 2)
        reasons = {r["text"]: r["reason"] for r in rejected}
        self.assertEqual(reasons["DOWNLOAD NOW"], "ad (filter lists)")
        self.assertEqual(reasons["Download"], "hidden")
        self.assertTrue(reasons["Home"].startswith("score"))


@unittest.skipIf(get_clearcote_executable() is None or not get_clearcote_executable().exists() or find_transfer_core() is None,
                 "Clearcote and transfer-core are needed")
class FollowInBrowserTests(unittest.TestCase):
    def test_clicks_through_mirror_and_countdown_to_the_file(self):
        from engine.browser_solver import set_ad_blocker, solver_daemon
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with TemporaryDirectory() as data_dir:
            blocker = AdBlocker(Path(data_dir))
            blocker.dir.mkdir(parents=True)
            (blocker.dir / "easylist.txt").write_text("||adhost.test^\n##.zq-slot-7\n", encoding="utf-8")
            set_ad_blocker(lambda: blocker)
            self.addCleanup(set_ad_blocker, lambda: None)
            result = solver_daemon.follow_download_sync(f"http://127.0.0.1:{server.server_port}/post", timeout_seconds=120)
        self.assertIsNone(result["error"], result)
        self.assertTrue(result["direct_url"].endswith("/file/game.zip"), result)
        self.assertEqual(result["filename"], "game.zip")
        self.assertGreaterEqual(len(result["hops"]), 3, result["hops"])
        self.assertIn("ad (filter lists)", {r["reason"] for r in result["rejected"]})


if __name__ == "__main__":
    unittest.main()
