from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import base64
import codecs
import json
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

from engine.bypass_vip import BypassVipClient
from engine.shortlink_resolver import RecursiveShortlinkResolver, HopResult
from engine.shortlink_rules import DeepParamDecoder, SpecializedRuleRegistry
from engine.service import EngineService


class ShortlinkBypasserSuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path("test_scratch_shortlink_suite")
        self.temp_dir.mkdir(exist_ok=True)
        self.param_decoder = DeepParamDecoder()
        self.rules = SpecializedRuleRegistry()

    def tearDown(self) -> None:
        import shutil
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_deep_param_decoder_base64_and_variations(self) -> None:
        target = "https://mega.nz/file/sample123#key456"

        # 1. Standard base64 in 'u'
        b64_val = base64.b64encode(target.encode("utf-8")).decode("utf-8")
        url_b64 = f"https://network-loop.com/.safe/redirect.html?u={b64_val}"
        self.assertEqual(self.param_decoder.decode(url_b64), target)

        # 2. Base64 in 'v' (Anchoreth style)
        url_v = f"https://anchoreth.com/r-adsh?t=i&v={b64_val}&s=Title"
        self.assertEqual(self.param_decoder.decode(url_v), target)

        # 3. Reversed Base64 (BASD style)
        rev_b64 = b64_val[::-1]
        url_rev = f"https://faucet-shortener.test/link?dest={rev_b64}"
        self.assertEqual(self.param_decoder.decode(url_rev), target)

        # 4. Hex-encoded ASCII
        hex_val = target.encode("utf-8").hex()
        url_hex = f"https://redirector.test/jump?link={hex_val}"
        self.assertEqual(self.param_decoder.decode(url_hex), target)

        # 5. ROT13
        rot13_val = codecs.encode(target, "rot_13")
        quoted_rot13 = urllib.parse.quote(rot13_val)
        url_rot13 = f"https://cpm-redirect.test/out?url={quoted_rot13}"
        self.assertEqual(self.param_decoder.decode(url_rot13), target)

        # 6. URL in path
        url_in_path = f"https://shortener.test/go/{target}"
        self.assertEqual(self.param_decoder.decode(url_in_path), target)

    def test_ouo_rule_transformation(self) -> None:
        ouo_url = "https://ouo.io/AbCdEf"
        transformed = self.rules.match_and_bypass(ouo_url)
        self.assertEqual(transformed, "https://ouo.io/xreallcygo/AbCdEf")

        ouo_press = "https://ouo.press/123456"
        transformed_press = self.rules.match_and_bypass(ouo_press)
        self.assertEqual(transformed_press, "https://ouo.io/xreallcygo/123456")

        # shrink-service.it de-loop
        shrink_url = "https://www.shrink-service.it/btn/gmFFCC"
        transformed_shrink = self.rules.match_and_bypass(shrink_url)
        self.assertEqual(transformed_shrink, "https://adshnk.com/gmFFCC")

    def test_rekonise_json_scraper(self) -> None:
        html_content = '''
        <!DOCTYPE html>
        <html>
        <head>
            <script>
                window.__INITIAL_STATE__ = {
                    "campaign": {
                        "id": "12345",
                        "target_url": "https://mediafire.com/file/my_archive.zip/file"
                    }
                };
            </script>
        </head>
        <body></body>
        </html>
        '''
        extracted = self.rules.parse_rekonise_campaign(html_content, "https://rekonise.com/sample-campaign")
        self.assertEqual(extracted, "https://mediafire.com/file/my_archive.zip/file")

    def test_bypass_vip_client_mock_and_offline_fallback(self) -> None:
        client = BypassVipClient()

        # Mock successful API response
        mock_response = json.dumps({"status": "success", "result": "https://gofile.io/d/resolved123"}).encode("utf-8")
        mock_http_resp = MagicMock()
        mock_http_resp.status = 200
        mock_http_resp.read.return_value = mock_response
        mock_http_resp.__enter__.return_value = mock_http_resp

        with patch("engine.route_http.urlopen", return_value=mock_http_resp):
            resolved = client.resolve("https://linkvertise.com/123/example")
            self.assertEqual(resolved, "https://gofile.io/d/resolved123")

        # Mock rate limit or server error response
        mock_err_resp = json.dumps({"status": "error", "message": "Rate limited"}).encode("utf-8")
        mock_http_err = MagicMock()
        mock_http_err.status = 200
        mock_http_err.read.return_value = mock_err_resp
        mock_http_err.__enter__.return_value = mock_http_err

        with patch("engine.route_http.urlopen", return_value=mock_http_err):
            resolved_err = client.resolve("https://linkvertise.com/123/example")
            self.assertIsNone(resolved_err)

        # Disabled client returns None immediately
        client.configure(enabled=False)
        self.assertIsNone(client.resolve("https://linkvertise.com/123/example"))

    def test_network_loop_adshrink_anchoreth_chain(self) -> None:
        resolver = RecursiveShortlinkResolver(max_hops=10)
        start_url = "https://network-loop.com/.safe/redirect.html?u=aHR0cHM6Ly93d3cuc2hyaW5rLXNlcnZpY2UuaXQvYnRuL2dtRkZDQw=="

        async def run_res():
            return await resolver.resolve_chain(start_url)

        # Keep this regression test deterministic and offline. The resolver's
        # first two hops are real parsing/rule behavior; the third-party API is
        # represented by the same safe terminal URL it would return.
        with patch.object(resolver.rules, "query_adshrink_api",
                          return_value="https://mega.nz/file/fixture#key"):
            final_url, hops = asyncio.run(run_res())
        self.assertTrue(final_url.startswith("https://mega.nz/file/"))
        self.assertGreaterEqual(len(hops), 3)
        strategies = [h.strategy for h in hops]
        self.assertIn("param_extract", strategies)
        self.assertIn("rule_rewrite", strategies)
        self.assertIn("adshrink_api_resolve", strategies)

    def test_service_shortlink_rpcs(self) -> None:
        service = EngineService(self.temp_dir / "service_data")
        try:
            # Test supported hosts listing
            hosts_info = service.dispatch("shortlink_list_supported_hosts", {})
            self.assertIn("catalog_hosts_count", hosts_info)
            self.assertGreater(hosts_info["catalog_hosts_count"], 100)
            self.assertIn("tier0_strategies", hosts_info)
            self.assertIn("tier1_specialized_rules", hosts_info)

            # Test bypass.vip configuration
            cfg = service.dispatch("shortlink_bypass_vip_config", {
                "base_url": "https://custom.bypass.test/api",
                "enabled": True,
                "api_key": "secret_token_123",
            })
            self.assertTrue(cfg["configured"])
            self.assertEqual(cfg["base_url"], "https://custom.bypass.test/api")
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
