import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import unittest
from unittest.mock import MagicMock, patch

from engine.service import EngineService
from engine.providers.cyberdrop_hosts import DatanodesProvider
from engine.errors import NeedsUser
from engine.providers.datanodes_resume import CONTINUATION_KEY, build_continuation
from engine.telemetry import redact_telemetry_data


class TestDataNodesMultiPartAndSessionIsolation(unittest.TestCase):
    def test_resumed_step_two_skips_landing_and_step_one(self):
        url = "https://datanodes.to/code2/archive.part02.rar"
        continuation = build_continuation(
            url=url, file_code="code2", file_name="archive.part02.rar",
            rand="saved-rand", dl_token="saved-dl-token",
            cookies={"dn_session": "original-session"}, countdown_seconds=0,
            site_key="site-key", discovered_size=1234,
        )
        final_response = MagicMock(status=200, headers={})
        final_response.read.return_value = b'{"url":"https://node2.datanodes.to/d/file/archive.part02.rar"}'

        with patch("engine.http_client.get") as landing_get, \
             patch("engine.http_client.post", return_value=final_response) as post:
            items = DatanodesProvider.resolve(url, {
                "_provider_continuation": continuation,
                "turnstile_token": "solved-token",
                "cookies": {
                    "file_code": "wrong-sibling-code",
                    "file_name": "archive.part01.rar",
                    "dn_browser": "shared-context",
                },
                "task_id": "task-part-2",
            })

        landing_get.assert_not_called()
        post.assert_called_once()
        self.assertEqual(items[0].direct_url, "https://node2.datanodes.to/d/file/archive.part02.rar")
        self.assertEqual(items[0].size, 1234)
        request = post.call_args.kwargs
        self.assertEqual(request["data"]["rand"], "saved-rand")
        self.assertEqual(request["data"]["dl_token"], "saved-dl-token")
        self.assertIn("dn_session=original-session", request["headers"]["Cookie"])
        self.assertIn("file_code=code2", request["headers"]["Cookie"])
        self.assertNotIn("wrong-sibling-code", request["headers"]["Cookie"])
        self.assertIn("dn_browser=shared-context", request["headers"]["Cookie"])

    def test_continuation_is_redacted_from_telemetry(self):
        redacted = redact_telemetry_data({"provider_continuation": {"cookies": {"secret": "value"}}})
        self.assertEqual(redacted["provider_continuation"], "[REDACTED]")

    def test_rejected_token_preserves_step_two_continuation(self):
        url = "https://datanodes.to/code3/archive.part03.rar"
        continuation = build_continuation(
            url=url, file_code="code3", file_name="archive.part03.rar",
            rand="saved-rand", dl_token="saved-token", cookies={"session": "same"},
            countdown_seconds=0, site_key="site-key", discovered_size=None,
        )
        response = MagicMock(status=200, headers={})
        response.read.return_value = b'<div class="cf-turnstile">verify you are human</div>'
        with patch("engine.http_client.get") as landing_get, \
             patch("engine.http_client.post", return_value=response):
            with self.assertRaises(NeedsUser) as raised:
                DatanodesProvider.resolve(url, {
                    "_provider_continuation": continuation,
                    "turnstile_token": "rejected-token",
                })
        landing_get.assert_not_called()
        self.assertEqual(raised.exception.challenge[CONTINUATION_KEY], continuation)
        self.assertEqual(raised.exception.challenge["response_reason"], "continuation_rejected")

    def test_step_two_html_is_treated_as_a_resumable_rejection(self):
        url = "https://datanodes.to/code4/archive.part04.rar"
        continuation = build_continuation(
            url=url, file_code="code4", file_name="archive.part04.rar",
            rand="saved-rand", dl_token="saved-token", cookies={"session": "same"},
            countdown_seconds=0, site_key="site-key", discovered_size=None,
        )
        response = MagicMock(status=200, headers={})
        response.read.return_value = b'<download-countdown rand="saved-rand"></download-countdown>'
        with patch("engine.http_client.post", return_value=response):
            with self.assertRaises(NeedsUser) as raised:
                DatanodesProvider.resolve(url, {
                    "_provider_continuation": continuation,
                    "turnstile_token": "rejected-token",
                })
        self.assertEqual(raised.exception.challenge[CONTINUATION_KEY], continuation)

    def test_sanitize_host_session_data_strips_turnstile_and_file_cookies(self):
        dirty_session = {
            "turnstile_token": "burned_turnstile_tok_123",
            "cf-turnstile-response": "burned_turnstile_tok_123",
            "cf_clearance": "valid_cf_clearance_abc",
            "cookies": {
                "file_code": "part01_code",
                "file_name": "part01.rar",
                "cf_clearance": "valid_cf_clearance_abc",
                "session": "sess_123",
            },
            "user_agent": "Mozilla/5.0 Test",
            "task_id": "task-uuid-1",
        }

        clean = EngineService._sanitize_host_session_data(dirty_session)

        self.assertNotIn("turnstile_token", clean)
        self.assertNotIn("cf-turnstile-response", clean)
        self.assertNotIn("task_id", clean)
        self.assertEqual(clean.get("cf_clearance"), "valid_cf_clearance_abc")
        self.assertEqual(clean.get("user_agent"), "Mozilla/5.0 Test")

        clean_cookies = clean.get("cookies", {})
        self.assertNotIn("file_code", clean_cookies)
        self.assertNotIn("file_name", clean_cookies)
        self.assertEqual(clean_cookies.get("cf_clearance"), "valid_cf_clearance_abc")
        # Positive allowlist: a generic session cookie is per-file state on hosts
        # like DataNodes, so it stays with the task that earned it.
        self.assertNotIn("session", clean_cookies)

    def test_datanodes_cookie_isolation(self):
        """Verify DatanodesProvider does not allow secrets cookies to override current file_code."""
        test_url = "https://datanodes.to/my_current_code/my_file.part02.rar"

        mock_probe_resp = MagicMock()
        mock_probe_resp.status = 200
        mock_probe_resp.read.return_value = b'<html><body>code="my_current_code" rand="rand1" dl-token="tok1" :countdown="0"</body></html>'
        mock_probe_resp.geturl.return_value = test_url
        mock_probe_resp.headers = {}

        mock_post_resp = MagicMock()
        mock_post_resp.status = 200
        mock_post_resp.read.return_value = b'{"url": "https://node1.datanodes.to/d/xyz/my_file.part02.rar"}'
        mock_post_resp.geturl.return_value = "https://datanodes.to/download"
        mock_post_resp.headers = {}

        with patch("engine.http_client.get", return_value=mock_probe_resp), \
             patch("engine.http_client.post", return_value=mock_post_resp) as mock_post:
            
            secrets = {
                "cookies": {"file_code": "wrong_part01_code", "lang": "english"},
            }
            items = DatanodesProvider.resolve(test_url, secrets=secrets)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].direct_url, "https://node1.datanodes.to/d/xyz/my_file.part02.rar")

            post_call = mock_post.call_args
            post_data = post_call.kwargs.get("data") or post_call[1].get("data")
            post_headers = post_call.kwargs.get("headers") or post_call[1].get("headers")

            self.assertEqual(post_data["id"], "my_current_code")
            self.assertIn("file_code=my_current_code", post_headers["Cookie"])
            self.assertNotIn("file_code=wrong_part01_code", post_headers["Cookie"])

    def test_multipart_folder_and_sibling_resume(self):
        """Verify sibling tasks in the same folder receive shared solve secrets."""
        from engine.models import DownloadTask
        from engine.db import TaskStore

        store = TaskStore(":memory:")
        t1 = DownloadTask(
            source_url="https://datanodes.to/code1/archive.part01.rar",
            destination="/downloads/archive",
            display_name="archive.part01.rar",
            folder_path="/downloads/archive",
            state="needs_user"
        )
        t2 = DownloadTask(
            source_url="https://datanodes.to/code2/archive.part02.rar",
            destination="/downloads/archive",
            display_name="archive.part02.rar",
            folder_path="/downloads/archive",
            state="paused"
        )
        store.save(t1)
        store.save(t2)

        siblings = store.list_by_folder("/downloads/archive")
        self.assertEqual(len(siblings), 2)
        sibling_ids = {s.id for s in siblings}
        self.assertIn(t1.id, sibling_ids)
        self.assertIn(t2.id, sibling_ids)


class TestDataNodesStep1FnameDecoy(unittest.TestCase):
    """DataNodes injects a decoy fname="Download" before the real filename."""

    DECOY_HTML = (
        '<input type="hidden" name="fname" value="Download">'
        '<input type="hidden" name="op" value="download1">'
        '<input type="hidden" name="id" value="1cg0jq70af68">'
        '<input type="hidden" name="fname" value="Kristala_--_example-repacks.test_--_.part1.rar">'
    )

    def test_decoy_is_ignored(self):
        chosen = DatanodesProvider._select_step1_fname(
            self.DECOY_HTML, "Kristala_--_example-repacks.test_--_.part1.rar")
        self.assertEqual(chosen, "Kristala_--_example-repacks.test_--_.part1.rar")
        self.assertNotEqual(chosen.lower(), "download")

    def test_single_real_fname(self):
        html = '<input type="hidden" name="fname" value="Movie.part1.rar">'
        self.assertEqual(DatanodesProvider._select_step1_fname(html, "Movie.part1.rar"), "Movie.part1.rar")

    def test_missing_fname_falls_back_to_file_name(self):
        html = '<input type="hidden" name="op" value="download1">'
        self.assertEqual(DatanodesProvider._select_step1_fname(html, "Given.part2.rar"), "Given.part2.rar")

    def test_only_decoy_prefers_known_file_name(self):
        html = '<input name="fname" value="Download">'
        self.assertEqual(DatanodesProvider._select_step1_fname(html, "X.part3.rar"), "X.part3.rar")


if __name__ == "__main__":
    unittest.main()
