"""The Rust core verifies against whichever checksum a provider publishes."""
from __future__ import annotations

import hashlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.backend_selection import BackendSelector
from engine.models import ResolvedItem
from engine.rust_backend import RustTransferBackend, find_transfer_core

FIXTURE = bytes(range(256)) * 400  # 100 KiB


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        start, end = 0, len(FIXTURE) - 1
        requested = self.headers.get("Range")
        if requested and requested.startswith("bytes="):
            raw_start, _, raw_end = requested[6:].partition("-")
            start = int(raw_start or 0)
            end = int(raw_end) if raw_end else len(FIXTURE) - 1
        body = FIXTURE[start:end + 1]
        self.send_response(206 if requested else 200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(FIXTURE)}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipIf(find_transfer_core() is None, "transfer-core binary is not built")
class RustChecksumTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/fixture.bin"

    def _item(self, checksum: str | None) -> ResolvedItem:
        return ResolvedItem("generic", self.url, "fixture.bin", size=len(FIXTURE),
                            direct_url=self.url, checksum=checksum)

    def _download(self, checksum: str | None) -> Path:
        with TemporaryDirectory() as root:
            path = RustTransferBackend(min_segment_size=16 * 1024).download(
                self._item(checksum), root)
            self.assertEqual(path.read_bytes(), FIXTURE)
            return path

    def test_every_published_algorithm_verifies(self):
        for algorithm in ("sha256", "sha1", "md5"):
            digest = hashlib.new(algorithm, FIXTURE).hexdigest()
            with self.subTest(algorithm=algorithm):
                self._download(f"{algorithm}:{digest}")

    def test_a_bare_digest_is_read_as_sha256(self):
        self._download(hashlib.sha256(FIXTURE).hexdigest())

    def test_a_wrong_digest_is_rejected_for_each_algorithm(self):
        for algorithm in ("sha256", "sha1", "md5"):
            wrong = hashlib.new(algorithm, b"not this file").hexdigest()
            with self.subTest(algorithm=algorithm), TemporaryDirectory() as root:
                with self.assertRaises(Exception):
                    RustTransferBackend(max_retries=0, min_segment_size=16 * 1024).download(
                        self._item(f"{algorithm}:{wrong}"), root)

    def test_an_unknown_algorithm_fails_rather_than_passing_silently(self):
        with TemporaryDirectory() as root:
            with self.assertRaises(Exception) as caught:
                RustTransferBackend(max_retries=0).download(
                    self._item("crc32:deadbeef"), root)
            self.assertIn("crc32", str(caught.exception))

    def test_selector_no_longer_excludes_rust_for_non_sha256(self):
        # Until the core learned these algorithms, an md5 checksum alone was
        # enough to push a transfer onto the Python transport.
        backend = RustTransferBackend()
        item = self._item("md5:" + hashlib.md5(FIXTURE).hexdigest())
        decision = BackendSelector().select(item, {"kind": "direct"}, {"rust": backend})
        self.assertTrue(decision.compatible, decision.reason)
        self.assertEqual(decision.backend, "rust")


if __name__ == "__main__":
    unittest.main()
