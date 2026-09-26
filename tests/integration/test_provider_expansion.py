from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import unittest
from email.message import Message
from unittest.mock import patch

from engine.providers.hosted import (CyberdropProvider, GoogleDriveProvider, KrakenfilesProvider,
                                     MediaFireProvider, OneFichierProvider, PixeldrainProvider)


def fake_fetch(body: bytes | str, final_url: str, content_type: str = "text/html"):
    headers = Message()
    headers["Content-Type"] = content_type
    return body.encode() if isinstance(body, str) else body, final_url, headers


class HostedProviderTests(unittest.TestCase):
    def test_mediafire_resolve_and_enumerate(self):
        body = '<a href="https://download.mediafire.com/test/file.zip">download</a>'
        with patch("engine.providers.hosted._fetch", return_value=fake_fetch(body, "https://www.mediafire.com/file/abc/file.html")):
            item = MediaFireProvider.resolve("https://www.mediafire.com/file/abc/file.html")[0]
            self.assertEqual(item.direct_url, "https://download.mediafire.com/test/file.zip")
            self.assertEqual(MediaFireProvider.enumerate("https://www.mediafire.com/file/abc/file.html")[0].metadata["type"], "file")

    def test_google_drive_folder_api_tree_and_selection(self):
        payload = {"files": [{"id": "f1", "name": "one.bin", "mimeType": "application/octet-stream", "size": "12"},
                             {"id": "d1", "name": "Folder", "mimeType": "application/vnd.google-apps.folder"}]}
        with patch("engine.providers.hosted._json", return_value=payload):
            items = GoogleDriveProvider.enumerate("https://drive.google.com/drive/folders/root", {"api_key": "fixture"})
        self.assertEqual([item.metadata["type"] for item in items], ["file", "folder"])
        self.assertEqual(GoogleDriveProvider.resolve("https://drive.google.com/file/d/f1/view")[0].item_id, "f1")

    def test_pixeldrain_list_tree_and_download(self):
        with patch("engine.providers.hosted._json", return_value={"value": {"files": [{"id": "abc", "name": "a.bin", "size": 3, "hash": "h"}]}}):
            items = PixeldrainProvider.enumerate("https://pixeldrain.com/l/list")
        with patch("engine.providers.hosted._json", side_effect=[{"value": {"files": [{"id": "abc", "name": "a.bin", "size": 3, "hash": "h"}]}},
                                                                    {"value": {"name": "a.bin", "size": 3, "hash": "h"}}]):
            resolved = PixeldrainProvider.resolve("https://pixeldrain.com/l/list", {"selected_item_ids": ["abc"]})
        self.assertEqual(items[0].relative_path, "a.bin")
        self.assertTrue(resolved[0].direct_url.endswith("/abc?download"))

    def test_onefichier_and_krakenfiles_html_resolution(self):
        one = '<a href="https://1fichier.com/dl/abc/file.bin">Download</a>'
        kraken = '<script>download_url: "https://krakenfiles.com/download/abc"</script>'
        with patch("engine.providers.hosted._fetch", return_value=fake_fetch(one, "https://1fichier.com/?abc")):
            self.assertIn("/dl/", OneFichierProvider.resolve("https://1fichier.com/?abc")[0].direct_url)
        with patch("engine.providers.hosted._fetch", return_value=fake_fetch(kraken, "https://krakenfiles.com/view/abc")):
            self.assertIn("/download/", KrakenfilesProvider.resolve("https://krakenfiles.com/view/abc")[0].direct_url)

    def test_cyberdrop_album_tree_and_selection(self):
        body = '<a href="https://files.cyberdrop.me/a.jpg">a</a><a href="https://files.cyberdrop.me/b.zip">b</a>'
        with patch("engine.providers.hosted._fetch", return_value=fake_fetch(body, "https://cyberdrop.me/a/album")):
            items = CyberdropProvider.enumerate("https://cyberdrop.me/a/album")
            selected = CyberdropProvider.resolve("https://cyberdrop.me/a/album", {"selected_item_ids": [items[1].item_id]})
        self.assertEqual(len(items), 2)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].display_name, "b.zip")


if __name__ == "__main__":
    unittest.main()
