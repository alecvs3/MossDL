from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.service import EngineService
from engine.providers.hosted import _headers


class HostSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.service = EngineService(self.data_dir)

    def tearDown(self) -> None:
        self.service.close()
        self.temp_dir.cleanup()

    def test_set_and_get_host_session(self) -> None:
        session = {
            "turnstile_token": "single_use_token_should_be_stripped",
            "cf-turnstile-response": "single_use_token_should_be_stripped",
            "cf_clearance": "clr_abc_456",
            "user_agent": "CustomBrowser/1.0",
        }
        self.service.set_host_session("DataNodes.TO", session, ttl=3600.0)

        # Retrieve normalized lowercase
        retrieved = self.service.get_host_session("datanodes.to")
        # Single-use challenge tokens must be stripped from warm host sessions
        self.assertNotIn("turnstile_token", retrieved)
        self.assertNotIn("cf-turnstile-response", retrieved)
        self.assertEqual(retrieved.get("cf_clearance"), "clr_abc_456")
        self.assertEqual(retrieved.get("user_agent"), "CustomBrowser/1.0")

        # Check persistence
        persisted = self.service.store.get_setting("provider_host_sessions", {})
        self.assertIn("datanodes.to", persisted)
        self.assertEqual(persisted["datanodes.to"]["cf_clearance"], "clr_abc_456")

    def test_sibling_clearance_auto_resume(self) -> None:
        """Verify sibling tasks sharing folder_path automatically inherit clearance and resume."""
        from engine.models import DownloadTask

        t1 = DownloadTask(
            source_url="https://datanodes.to/code1/part1.rar",
            destination=str(self.data_dir),
            display_name="part1.rar",
            folder_path="/downloads/pkg",
            state="needs_user"
        )
        t2 = DownloadTask(
            source_url="https://datanodes.to/code2/part2.rar",
            destination=str(self.data_dir),
            display_name="part2.rar",
            folder_path="/downloads/pkg",
            state="needs_user"
        )
        self.service.store.save(t1)
        self.service.store.save(t2)

        # When clearance is set for datanodes.to, update siblings in the same folder
        session = {
            "cf_clearance": "inherited_cf_clearance_999",
            "user_agent": "Mozilla/5.0 WarmSession",
        }
        self.service.set_host_session("datanodes.to", session)

        siblings = self.service.store.list_by_folder("/downloads/pkg")
        for sib in siblings:
            if sib.state in {"needs_user", "paused"}:
                self.service.dispatch("resume_task", {"id": sib.id, "secrets": session})

        updated_t1 = self.service.store.get(t1.id)
        updated_t2 = self.service.store.get(t2.id)
        self.assertIn(updated_t1.state, {"queued", "downloading", "resolving"})
        self.assertIn(updated_t2.state, {"queued", "downloading", "resolving"})

    def test_expired_host_session(self) -> None:
        session = {"cf_clearance": "expired_clearance"}
        self.service.set_host_session("fuckingfast.co", session, ttl=-10.0)

        retrieved = self.service.get_host_session("fuckingfast.co")
        self.assertEqual(retrieved, {})

    def test_clear_host_session(self) -> None:
        session = {"cf_clearance": "active_clr"}
        self.service.set_host_session("datanodes.to", session, ttl=3600.0)
        self.assertTrue(bool(self.service.get_host_session("datanodes.to")))

        self.service.clear_host_session("datanodes.to")
        self.assertEqual(self.service.get_host_session("datanodes.to"), {})

    def test_hosted_headers_injection(self) -> None:
        secrets = {
            "cf_clearance": "clearance_cookie_token_999",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TestBrowser",
            "cookie": "lang=en; test=1;",
        }
        headers = _headers(secrets)
        self.assertEqual(headers.get("User-Agent"), "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TestBrowser")
        self.assertIn("cf_clearance=clearance_cookie_token_999", headers.get("Cookie", ""))
        self.assertIn("lang=en", headers.get("Cookie", ""))


if __name__ == "__main__":
    unittest.main()
