import unittest
from unittest.mock import MagicMock, patch
import tempfile
import shutil

from engine.service import EngineService, headless_required_scope
from engine.shortlink_resolver import HopResult


class TestInteractiveResolver(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.service = EngineService(self.temp_dir)

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_scope_authorization(self):
        # Verify that shortlink_resolve is authorized as read scope
        self.assertEqual(headless_required_scope("shortlink_resolve"), "read")
        self.assertEqual(headless_required_scope("shortlink_resolve_recursive"), "read")

    @patch("engine.shortlink_resolver.RecursiveShortlinkResolver.resolve_chain")
    def test_shortlink_resolve_rpc_contract(self, mock_resolve_chain):
        # Mock resolve_chain returning hops
        mock_hops = [
            HopResult(
                hop_number=1,
                input_url="https://shrink-service.it/btn/abc1234",
                output_url="https://adshnk.com/abc1234",
                strategy="specialized_rule_adshrink",
                delay_seconds=0.0,
                captcha_encountered=False,
                status="resolved"
            ),
            HopResult(
                hop_number=2,
                input_url="https://adshnk.com/abc1234",
                output_url="https://www.mediafire.com/file/dl99/sample.zip",
                strategy="deep_param_query",
                delay_seconds=0.1,
                captcha_encountered=False,
                status="resolved"
            )
        ]

        async def _mock_coro(url, task_id=None, **kwargs):
            return "https://www.mediafire.com/file/dl99/sample.zip", mock_hops

        mock_resolve_chain.side_effect = _mock_coro

        result = self.service.dispatch("shortlink_resolve", {"url": "https://shrink-service.it/btn/abc1234"})

        self.assertEqual(result["final_url"], "https://www.mediafire.com/file/dl99/sample.zip")
        self.assertEqual(len(result["hops"]), 2)
        self.assertEqual(result["hops"][0]["hop_number"], 1)
        self.assertEqual(result["hops"][0]["strategy"], "specialized_rule_adshrink")
        self.assertEqual(result["hops"][1]["output_url"], "https://www.mediafire.com/file/dl99/sample.zip")

    def test_storage_host_signatures(self):
        from engine.shortlink_resolver import _KNOWN_STORAGE_HOSTS
        self.assertIn("mediafire.com", _KNOWN_STORAGE_HOSTS)
        self.assertIn("1fichier.com", _KNOWN_STORAGE_HOSTS)
        self.assertIn("mega.nz", _KNOWN_STORAGE_HOSTS)
        self.assertIn("drive.google.com", _KNOWN_STORAGE_HOSTS)
        self.assertIn("vikingfile.com", _KNOWN_STORAGE_HOSTS)


if __name__ == "__main__":
    unittest.main()
