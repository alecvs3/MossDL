from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import unittest

from engine.linkgrabber import bare_url, extract_urls


class BareUrlTests(unittest.TestCase):
    def test_scheme_less_links_are_found(self) -> None:
        text = "see rutracker.org/forum/viewtopic.php?t=1, www.example.com and https://pixeldrain.com/u/8f"
        self.assertEqual(extract_urls(text), [
            "https://rutracker.org/forum/viewtopic.php?t=1",
            "https://www.example.com",
            "https://pixeldrain.com/u/8f",
        ])

    def test_file_names_and_versions_are_not_links(self) -> None:
        for token in ("movie.mp4", "setup.exe", "main.py", "readme.md", "node.js", "v1.2.3", "archive.zip"):
            self.assertIsNone(bare_url(token), token)
        self.assertEqual(extract_urls("mail bob@mail.com about notes.txt"), [])

    def test_extension_like_tlds_need_a_path(self) -> None:
        self.assertIsNone(bare_url("site.zip"))
        self.assertEqual(bare_url("site.zip/files"), "https://site.zip/files")
        self.assertEqual(bare_url("www.example.sh"), "https://www.example.sh")


if __name__ == "__main__":
    unittest.main()
