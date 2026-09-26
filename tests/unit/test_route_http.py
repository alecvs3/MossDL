"""Nothing the engine fetches leaves on the real IP while a route is selected.

A local SOCKS5 server stands in for the route. Every request the engine makes
for crawls, favicons, resolvers and plugins must pass through it; the origin
server records who connected, so a direct connection is caught, not assumed.
"""
from __future__ import annotations

import re
import socket
import socketserver
import struct
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import http_client, route_http  # noqa: E402
from engine.route_http import RouteUnavailable, proxy_url_for  # noqa: E402


class _Origin(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"<html><head><title>ok</title></head><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _Socks5(socketserver.BaseRequestHandler):
    """CONNECT-only SOCKS5, no auth; records the target it was asked for."""

    targets: list[str] = []

    def handle(self):
        conn = self.request
        _ver, count = conn.recv(2)
        conn.recv(count)
        conn.sendall(b"\x05\x00")
        _ver, _cmd, _rsv, atyp = conn.recv(4)
        if atyp == 3:
            host = conn.recv(conn.recv(1)[0]).decode()
        else:
            host = socket.inet_ntoa(conn.recv(4))
        port = struct.unpack(">H", conn.recv(2))[0]
        _Socks5.targets.append(f"{host}:{port}")
        upstream = socket.create_connection(("127.0.0.1" if host == "origin.test" else host, port))
        conn.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack(">H", port))
        try:
            self._pipe(conn, upstream)
        finally:
            upstream.close()

    @staticmethod
    def _pipe(a, b):
        def forward(src, dst):
            try:
                while data := src.recv(65536):
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        t = threading.Thread(target=forward, args=(b, a), daemon=True)
        t.start()
        forward(a, b)
        t.join(5)


class _ThreadingTCP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class RouteRulesTests(unittest.TestCase):
    def tearDown(self):
        route_http.set_active_route("direct", None)

    def test_kernel_routes_need_no_proxy(self):
        for kind in ("direct", "system_vpn", "wireguard"):
            self.assertIsNone(proxy_url_for({"kind": kind}))

    def test_core_tunnel_is_a_local_socks_port_with_remote_dns(self):
        self.assertEqual(proxy_url_for({"kind": "wireguard", "local_proxy": "127.0.0.1:41000"}),
                         "socks5h://127.0.0.1:41000")

    def test_socks_keeps_dns_on_the_proxy(self):
        self.assertEqual(proxy_url_for({"kind": "socks5", "endpoint": "socks5://10.0.0.2:1080"}), "socks5h://10.0.0.2:1080")

    def test_an_unusable_route_blocks_instead_of_going_direct(self):
        route_http.set_active_route("gone", None, error="route profile is no longer configured")
        with self.assertRaises(RouteUnavailable):
            route_http.urlopen("http://127.0.0.1:9/", timeout=1)

    def test_a_task_route_overrides_the_active_route_in_its_own_context(self):
        import contextvars
        route_http.set_active_route("proxy", "http://127.0.0.1:3128")
        ctx = contextvars.copy_context()
        self.assertIsNone(ctx.run(lambda: (route_http.bind_task_route(None), route_http.active_proxy())[1]))
        self.assertEqual(route_http.active_proxy(), "http://127.0.0.1:3128", "binding leaked out of its context")

    def test_browser_proxy_form(self):
        self.assertEqual(route_http.playwright_proxy("socks5h://127.0.0.1:41000"), {"server": "socks5://127.0.0.1:41000"})
        self.assertEqual(route_http.playwright_proxy("http://u:p%40ss@10.0.0.1:8080"),
                         {"server": "http://10.0.0.1:8080", "username": "u", "password": "p@ss"})
        with self.assertRaises(RouteUnavailable):
            route_http.playwright_proxy("socks5h://u:p@10.0.0.1:1080")


class LeakTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin = _serve(ThreadingHTTPServer(("127.0.0.1", 0), _Origin))
        cls.socks = _serve(_ThreadingTCP(("127.0.0.1", 0), _Socks5))
        cls.port = cls.origin.server_port
        cls.url = f"http://origin.test:{cls.port}/page"

    @classmethod
    def tearDownClass(cls):
        for server in (cls.origin, cls.socks):
            server.shutdown()
            server.server_close()

    def setUp(self):
        _Socks5.targets.clear()
        route_http.set_active_route("test-socks", f"socks5h://127.0.0.1:{self.socks.server_address[1]}")
        self.addCleanup(route_http.set_active_route, "direct", None)

    def assert_routed(self):
        # origin.test only exists on the far side of the proxy: reaching it at
        # all proves the request went through, and by name (remote DNS).
        self.assertIn(f"origin.test:{self.port}", _Socks5.targets)

    def test_urllib_callers(self):
        with route_http.urlopen(self.url, timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.assert_routed()

    def test_http_client_both_backends(self):
        self.assertEqual(http_client.request("GET", self.url, timeout=5).status, 200)
        self.assert_routed()
        _Socks5.targets.clear()
        self.assertEqual(http_client._urllib_fallback("GET", self.url, None, None, {}, {}, 5, False,
                                                      route_http.active_proxy()).status, 200)
        self.assert_routed()

    def test_page_crawl(self):
        from engine.service import EngineService
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as data_dir:
            service = EngineService(data_dir)
            try:
                route_http.set_active_route("test-socks", f"socks5h://127.0.0.1:{self.socks.server_address[1]}")
                service.dispatch("crawl_page_matrix", {"url": self.url})
            finally:
                service.close()
        self.assert_routed()


class ServiceRouteSyncTests(unittest.TestCase):
    def test_selecting_and_removing_a_route_repoints_the_engine(self):
        from tempfile import TemporaryDirectory
        from engine.service import EngineService
        with TemporaryDirectory() as data_dir:
            service = EngineService(data_dir)
            self.addCleanup(route_http.set_active_route, "direct", None)
            try:
                service.dispatch("route_save", {"id": "office", "kind": "socks5", "endpoint": "socks5://10.0.0.2:1080"})
                service.dispatch("ui_settings_update", {"settings": {"routes": {"activeLocation": "office"}}})
                self.assertEqual(route_http.active_proxy(), "socks5h://10.0.0.2:1080")
                service.dispatch("route_delete", {"profile_id": "office"})
                with self.assertRaises(RouteUnavailable, msg="a deleted route must block, not fall back to direct"):
                    route_http.active_proxy()
            finally:
                service.close()


class NoDirectSocketsGuard(unittest.TestCase):
    """New code must not open its own connections around the route."""

    # Deliberately direct: the solver download and plugin import come from
    # GitHub as app infrastructure, FlareSolverr is a local service, and
    # notifications go to the user's own webhook. routes.py measures routes.
    ALLOWED = {"route_http.py", "routes.py", "clearcote_manager.py", "cyberdrop_import.py",
               "flaresolverr_client.py", "notifications.py"}
    PATTERN = re.compile(r"urllib\.request\.urlopen\(|(?<![\w.])urlopen\(|urllib\.request\.build_opener\(|"
                         r"cffi_requests\.(get|post)\((?![^)]*proxies=)")

    def test_engine_uses_the_route_helper(self):
        offenders = []
        for path in (ROOT / "engine").rglob("*.py"):
            if path.name in self.ALLOWED:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self.PATTERN.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "use engine.route_http (or pass proxies=) instead")


if __name__ == "__main__":
    unittest.main()
