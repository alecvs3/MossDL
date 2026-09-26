"""The Rust core honours a route profile's proxy, end to end through the binary.

These tests drive the real transfer-core executable rather than a stub: the
point of the phase was that a proxied route reaches the wire, and only the
binary can show that.
"""
from __future__ import annotations

import hashlib
import socket
import struct
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.models import ResolvedItem
from engine.rust_backend import RustTransferBackend, find_transfer_core

FIXTURE = bytes(range(256)) * 512  # 128 KiB, enough for the core to segment

SOCKS_VERSION = 5
NO_AUTHENTICATION = 0
ADDRESS_IPV4 = 1
REQUEST_GRANTED = 0


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


class _Socks5Proxy:
    """A CONNECT-only SOCKS5 proxy that records what it was asked to reach.

    Small enough to be obviously correct, and it answers the question a dead
    proxy cannot: that the core really tunnels rather than merely failing.
    """

    def __init__(self) -> None:
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.targets: list[tuple[str, int]] = []
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self) -> None:
        self.listener.close()

    def _accept_loop(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    @staticmethod
    def _relay(source: socket.socket, sink: socket.socket) -> None:
        # Half-duplex: only the write side is shut down, so the peer can finish
        # sending before anything closes.
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                sink.sendall(data)
        except OSError:
            pass
        finally:
            try:
                sink.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _serve(self, client: socket.socket) -> None:
        upstream = None
        try:
            offered_methods = client.recv(2)[1]
            client.recv(offered_methods)
            client.sendall(bytes([SOCKS_VERSION, NO_AUTHENTICATION]))
            header = client.recv(4)
            if header[3] == ADDRESS_IPV4:
                host = socket.inet_ntoa(client.recv(4))
            else:
                host = client.recv(client.recv(1)[0]).decode()
            port = struct.unpack("!H", client.recv(2))[0]
            self.targets.append((host, port))
            upstream = socket.create_connection((host, port), timeout=5)
            client.sendall(bytes([SOCKS_VERSION, REQUEST_GRANTED, 0, ADDRESS_IPV4])
                           + socket.inet_aton("0.0.0.0") + struct.pack("!H", 0))
            forward = threading.Thread(target=self._relay, args=(client, upstream), daemon=True)
            forward.start()
            self._relay(upstream, client)
            forward.join(timeout=5)
        except OSError:
            pass
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass


def _closed_port() -> int:
    """A port nothing is listening on, so a proxy hop there cannot succeed."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@unittest.skipIf(find_transfer_core() is None, "transfer-core binary is not built")
class RustProxyRouteTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/fixture.bin"

    def _item(self) -> ResolvedItem:
        return ResolvedItem("generic", self.url, "fixture.bin",
                            size=len(FIXTURE), direct_url=self.url)

    def test_direct_route_downloads_the_file(self):
        with TemporaryDirectory() as root:
            path = RustTransferBackend(min_segment_size=16 * 1024).download(
                self._item(), root, route_profile={"kind": "direct"})
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             hashlib.sha256(FIXTURE).hexdigest())

    def test_system_vpn_route_is_not_proxied(self):
        # The kernel routes these, so the core should connect straight out and
        # the transfer should behave exactly like a direct one.
        with TemporaryDirectory() as root:
            path = RustTransferBackend(min_segment_size=16 * 1024).download(
                self._item(), root, route_profile={"kind": "system_vpn", "endpoint": "wg0"})
            self.assertEqual(path.read_bytes(), FIXTURE)

    def test_socks_proxy_carries_the_whole_transfer(self):
        # The proof that the route is honoured rather than merely fatal: every
        # segment connection arrives at the proxy, and the bytes still match.
        proxy = _Socks5Proxy()
        self.addCleanup(proxy.close)
        with TemporaryDirectory() as root:
            path = RustTransferBackend(max_retries=1, min_segment_size=16 * 1024).download(
                self._item(), root,
                route_profile={"kind": "socks5", "endpoint": f"127.0.0.1:{proxy.port}"})
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             hashlib.sha256(FIXTURE).hexdigest())
        self.assertTrue(proxy.targets, "the transfer never reached the proxy")
        self.assertTrue(all(target[1] == self.server.server_port for target in proxy.targets),
                        f"a connection bypassed the proxy: {proxy.targets}")

    def test_a_dead_proxy_fails_instead_of_going_direct(self):
        # If the core ignored the proxy it would reach the origin and quietly
        # succeed, which is the leak this phase had to rule out.
        endpoint = f"127.0.0.1:{_closed_port()}"
        with TemporaryDirectory() as root:
            with self.assertRaises(Exception):
                RustTransferBackend(max_retries=0, min_segment_size=16 * 1024).download(
                    self._item(), root, route_profile={"kind": "socks5", "endpoint": endpoint})
            self.assertEqual(list(Path(root).iterdir()), [], "a file appeared despite the dead route")

    def test_an_unusable_proxy_url_is_reported_not_ignored(self):
        with TemporaryDirectory() as root:
            with self.assertRaises(Exception):
                RustTransferBackend(max_retries=0).download(
                    self._item(), root,
                    route_profile={"kind": "http_proxy", "endpoint": "http://"})


if __name__ == "__main__":
    unittest.main()
