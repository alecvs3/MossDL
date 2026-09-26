from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine.transport_pool import PooledTransportManager


class _RangeProbeHandler(BaseHTTPRequestHandler):
    payload_size = 256

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(self.payload_size))
        self.send_header("ETag", '"fixture-v1"')
        # Deliberately omit Accept-Ranges: this is the CDN behavior that
        # previously made the production probe fall back to one stream.
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        value = self.headers.get("Range", "")
        if value == "bytes=0-0":
            self.send_response(206)
            self.send_header("Content-Length", "1")
            self.send_header("Content-Range", f"bytes 0-0/{self.payload_size}")
            self.send_header("ETag", '"fixture-v1"')
            self.end_headers()
            self.wfile.write(b"x")
            return
        self.send_response(200)
        self.send_header("Content-Length", str(self.payload_size))
        self.end_headers()
        self.wfile.write(b"x" * self.payload_size)

    def log_message(self, *_args) -> None:
        return


class TransportProbeTests(unittest.TestCase):
    def test_head_without_accept_ranges_validates_byte_ranges(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RangeProbeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            manager = PooledTransportManager()
            try:
                result = manager.probe(f"http://127.0.0.1:{server.server_port}/file")
            finally:
                manager.close()
            self.assertEqual(result["size"], _RangeProbeHandler.payload_size)
            self.assertTrue(result["ranges"])
            self.assertEqual(result["status_code"], 206)
            self.assertEqual(result["validator"], '"fixture-v1"')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
