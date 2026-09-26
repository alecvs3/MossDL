from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import os
import unittest
from pathlib import Path

from engine.errors import EngineError
from engine.plugins import PluginRegistry
from engine.provider_compatibility import compare_plugin_ecosystems


ROOT = _ROOT


class OptInProviderLiveMatrixTests(unittest.TestCase):
    """Provider smoke tests enabled only with TRANSFER_MANAGER_LIVE=1.

    Supply a URL per provider, for example
    TRANSFER_MANAGER_LIVE_URL_MEGA=https://mega.nz/folder/...
    TRANSFER_MANAGER_LIVE_URL_MEDIAFIRE=https://...
    Missing URLs are intentionally skipped, so CI remains offline and a
    provider is never treated as production-complete merely because its
    manifest loads.
    """

    def test_configured_provider_urls(self):
        if os.environ.get("TRANSFER_MANAGER_LIVE") != "1":
            self.skipTest("set TRANSFER_MANAGER_LIVE=1 for opt-in provider smoke tests")
        records = compare_plugin_ecosystems(ROOT / "plugins", ROOT / "clone_reference" / "cyberdrop-dl")
        configured = [record for record in records if record.cyberdrop_overlap and os.environ.get(record.live_url_env)]
        if not configured:
            self.skipTest("no TRANSFER_MANAGER_LIVE_URL_<PROVIDER> values supplied")

        registry = PluginRegistry()
        try:
            for record in configured:
                url = os.environ[record.live_url_env]
                with self.subTest(provider=record.provider_id, url=url):
                    self.assertEqual(registry.provider_for(url), record.provider_id)
                    try:
                        items = registry.enumerate(url)
                    except EngineError as exc:
                        self.fail(f"{record.provider_id} live fixture failed: {exc}")
                    self.assertTrue(items, f"{record.provider_id} returned no items")
                    for item in items:
                        self.assertTrue(item.provider == record.provider_id)
                        self.assertTrue(item.display_name or item.direct_url)
        finally:
            registry.close()


if __name__ == "__main__":
    unittest.main()

