from __future__ import annotations

import unittest

from engine.models import ResolvedItem
from engine.rust_backend import RustTransferBackend


class RustBackendHeaderTests(unittest.TestCase):
    def test_resolved_cookies_reach_the_transport(self) -> None:
        item = ResolvedItem(
            "datanodes", "https://datanodes.to/file", "file.rar",
            direct_url="https://tunnel5.dlproxy.uk/file",
            headers={"Referer": "https://datanodes.to/"},
        )
        item.cookies = {"file_code": "abc", "session": "isolated-lane"}

        headers = RustTransferBackend._request_headers(item)

        self.assertEqual(headers["Referer"], "https://datanodes.to/")
        self.assertEqual(headers["Cookie"], "file_code=abc; session=isolated-lane")

    def test_explicit_cookie_header_wins_case_insensitively(self) -> None:
        item = ResolvedItem(
            "generic", "https://example.test/file", "file.bin",
            direct_url="https://example.test/file",
            headers={"cookie": "signed=explicit"},
        )
        item.cookies = {"signed": "derived"}

        headers = RustTransferBackend._request_headers(item)

        self.assertEqual(headers["cookie"], "signed=explicit")
        self.assertNotIn("Cookie", headers)


if __name__ == "__main__":
    unittest.main()
