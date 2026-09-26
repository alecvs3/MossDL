"""The Rust core decrypts MEGA-family ciphertext exactly as the Python transport does.

This drives the real binary against ciphertext produced by PyCryptodome, which
is what the Python transport uses. Agreement between the two implementations is
the whole point: a decryption bug would produce a file that looks complete,
passes its size check, and is quietly corrupt.
"""
from __future__ import annotations

import hashlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.models import ResolvedItem
from engine.rust_backend import RustTransferBackend, find_transfer_core

try:
    from engine.custom_downloader import CustomAsyncBackend
    HAVE_PYCRYPTO = True
except Exception:  # pragma: no cover - exercised only where the dep is absent
    HAVE_PYCRYPTO = False

# A MEGA file key: eight 32-bit words, the first four XORed with the last four
# to form the AES key and words four and five forming the nonce.
KEY_A32 = [0x00112233, 0x44556677, 0x8899AABB, 0xCCDDEEFF,
           0x01020304, 0x05060708, 0xDEADBEEF, 0xFEEDFACE]

PLAINTEXT = bytes((index * 31 + 7) % 256 for index in range(300_000))

_ciphertext: bytes = b""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        start, end = 0, len(_ciphertext) - 1
        requested = self.headers.get("Range")
        if requested and requested.startswith("bytes="):
            raw_start, _, raw_end = requested[6:].partition("-")
            start = int(raw_start or 0)
            end = int(raw_end) if raw_end else len(_ciphertext) - 1
        body = _ciphertext[start:end + 1]
        self.send_response(206 if requested else 200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"mega-fixture"')
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(_ciphertext)}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipIf(find_transfer_core() is None, "transfer-core binary is not built")
@unittest.skipUnless(HAVE_PYCRYPTO, "PyCryptodome is required to build the fixture")
class RustMegaTransformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _ciphertext
        # CTR is symmetric, so the Python transport's own cipher produces the
        # ciphertext the engine would be served.
        cipher = CustomAsyncBackend._mega_ctr_cipher({"key_a32": KEY_A32})
        _ciphertext = cipher.decrypt(PLAINTEXT)
        assert _ciphertext != PLAINTEXT

    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/encrypted.bin"

    def _item(self) -> ResolvedItem:
        item = ResolvedItem("mega", self.url, "movie.mkv",
                            size=len(_ciphertext), direct_url=self.url)
        item.postprocess = {"type": "mega-ctr", "key_a32": KEY_A32, "size": len(PLAINTEXT)}
        return item

    def test_segmented_transfer_decrypts_to_the_original_bytes(self):
        # Small segments force several workers, each seeking its own keystream:
        # the case where an off-by-one in the counter would corrupt the file.
        with TemporaryDirectory() as root:
            path = RustTransferBackend(max_segments=8, min_segment_size=32 * 1024).download(
                self._item(), root)
            self.assertEqual(path.read_bytes(), PLAINTEXT)

    def test_single_stream_transfer_decrypts_to_the_original_bytes(self):
        # min_segment_size above the file keeps it on the single-stream path.
        with TemporaryDirectory() as root:
            path = RustTransferBackend(min_segment_size=8 * 1024 * 1024).download(
                self._item(), root)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             hashlib.sha256(PLAINTEXT).hexdigest())

    def test_the_two_transports_agree(self):
        # The comparison that matters: identical output from both engines.
        import asyncio

        from engine.limits import ResourceManager, SchedulerPolicy

        with TemporaryDirectory() as rust_root, TemporaryDirectory() as python_root:
            rust_path = RustTransferBackend(max_segments=4, min_segment_size=64 * 1024).download(
                self._item(), rust_root)
            backend = CustomAsyncBackend(ResourceManager(SchedulerPolicy(
                min_segment_size=64 * 1024, max_segments_per_file=4)))
            python_path = asyncio.run(backend.download(self._item(), python_root))
            self.assertEqual(Path(rust_path).read_bytes(), Path(python_path).read_bytes())

    def test_an_unknown_transform_is_refused_before_transferring(self):
        item = self._item()
        item.postprocess = {"type": "rot13", "key_a32": KEY_A32}
        with TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError) as caught:
                RustTransferBackend().download(item, root)
            self.assertIn("rot13", str(caught.exception))
            self.assertEqual(list(Path(root).iterdir()), [], "bytes were written anyway")


if __name__ == "__main__":
    unittest.main()
