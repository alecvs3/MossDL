from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import hashlib
import os
import shutil
import tempfile
import unittest
import urllib.request
from pathlib import Path

from engine.providers.hosted import (CyberdropProvider, GoogleDriveProvider, KrakenfilesProvider,
                                     MediaFireProvider, OneFichierProvider, PixeldrainProvider)


LIVE = os.environ.get("TRANSFER_MANAGER_LIVE") == "1"


class LiveProviderSmokeTests(unittest.TestCase):
    """Opt-in real-provider checks; never runs against arbitrary search results."""

    def setUp(self):
        if not LIVE:
            self.skipTest("set TRANSFER_MANAGER_LIVE=1 to enable live provider smoke tests")
        self.root = Path(tempfile.mkdtemp(prefix="transfer-manager-live-"))

    def tearDown(self):
        if hasattr(self, "root"):
            shutil.rmtree(self.root, ignore_errors=True)

    def _check(self, env_name: str, provider, allow_tree: bool = False):
        url = os.environ.get(env_name)
        if not url:
            self.skipTest(f"{env_name} is not configured")
        items = provider.enumerate(url) if allow_tree else provider.resolve(url)
        self.assertTrue(items)
        files = [item for item in items if (item.metadata or {}).get("type", "file") == "file"]
        self.assertTrue(files)
        item = files[0]
        self.assertTrue(item.direct_url)
        max_bytes = int(os.environ.get("TRANSFER_MANAGER_LIVE_MAX_BYTES", str(10 * 1024 * 1024)))
        destination = self.root / (item.display_name or "download")
        digest = hashlib.sha256()
        total = 0
        with urllib.request.urlopen(item.direct_url, timeout=30) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
                if not chunk: break
                total += len(chunk)
                if total > max_bytes: self.fail("live fixture exceeded configured size limit")
                digest.update(chunk); output.write(chunk)
        expected_size = os.environ.get(env_name.replace("_URL", "_SIZE"))
        expected_hash = os.environ.get(env_name.replace("_URL", "_SHA256"))
        if expected_size: self.assertEqual(total, int(expected_size))
        if expected_hash: self.assertEqual(digest.hexdigest().lower(), expected_hash.lower())

    def test_mediafire(self): self._check("MEDIAFIRE_TEST_URL", MediaFireProvider)
    def test_google_drive(self): self._check("GOOGLE_DRIVE_TEST_URL", GoogleDriveProvider)
    def test_pixeldrain(self): self._check("PIXELDRAIN_TEST_URL", PixeldrainProvider)
    def test_onefichier(self): self._check("ONEFICHIER_TEST_URL", OneFichierProvider)
    def test_krakenfiles(self): self._check("KRAKENFILES_TEST_URL", KrakenfilesProvider)
    def test_cyberdrop(self): self._check("CYBERDROP_TEST_URL", CyberdropProvider, allow_tree=True)


if __name__ == "__main__":
    unittest.main()
