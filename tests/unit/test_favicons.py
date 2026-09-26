from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tempfile
import unittest

from engine.favicons import FaviconCache, _sniff, normalize_host

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FaviconCacheTests(unittest.TestCase):
    def test_host_normalization(self) -> None:
        self.assertEqual(normalize_host("https://www.Example.org/path"), "example.org")
        self.assertEqual(normalize_host("cdn.example.org:8443"), "cdn.example.org")
        self.assertIsNone(normalize_host("localhost"))
        self.assertIsNone(normalize_host(""))

    def test_only_real_images_are_accepted(self) -> None:
        self.assertEqual(_sniff(PNG, "text/html"), "image/png")
        self.assertEqual(_sniff(b"<svg xmlns='x'/>", None), "image/svg+xml")
        self.assertIsNone(_sniff(b"<!doctype html><title>login</title>", "image/x-icon"))

    def test_fetch_once_then_serve_from_disk_including_misses(self) -> None:
        cache = FaviconCache(Path(tempfile.mkdtemp()))
        calls: list[str] = []

        def fake_fetch(host: str):
            calls.append(host)
            return {"mime": "image/png", "data": PNG} if host == "a.test" else {"mime": None, "data": b""}

        cache._fetch = fake_fetch  # type: ignore[method-assign]
        first = cache.get_many(["a.test", "https://www.a.test/x", "b.test"])
        self.assertTrue(first["a.test"].startswith("data:image/png;base64,"))
        self.assertEqual(first["https://www.a.test/x"], first["a.test"])
        self.assertIsNone(first["b.test"])
        cache.get_many(["a.test", "b.test"])
        self.assertEqual(sorted(calls), ["a.test", "b.test"], "hits and misses are both cached")

    def test_declared_icon_is_tried_first_then_parent_domains(self) -> None:
        cache = FaviconCache(Path(tempfile.mkdtemp()))
        cache.remember_hint("https://dl.mirror.example.org/page", "https://dl.mirror.example.org/static/icon.png")
        self.assertEqual(cache._candidates("dl.mirror.example.org"), [
            "https://dl.mirror.example.org/static/icon.png",
            "https://dl.mirror.example.org/favicon.ico",
            "https://mirror.example.org/favicon.ico",
            "https://example.org/favicon.ico",
        ])


if __name__ == "__main__":
    unittest.main()
