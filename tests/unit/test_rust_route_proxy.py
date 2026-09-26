"""Route profiles that the Rust transport turns into a proxy (or deliberately does not)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.backend_selection import BackendSelector
from engine.models import ResolvedItem
from engine.rust_backend import ROUTE_KINDS, RustTransferBackend


class RouteProxyTests(unittest.TestCase):
    def test_kernel_routed_kinds_get_no_proxy(self):
        # The OS already sends these connections the right way; adding a proxy
        # would be a second, wrong hop.
        for kind in ("direct", "system_vpn", "wireguard"):
            self.assertIsNone(RustTransferBackend.route_proxy({"kind": kind, "endpoint": "ignored"}), kind)

    def test_missing_profile_is_direct(self):
        self.assertIsNone(RustTransferBackend.route_proxy(None))
        self.assertIsNone(RustTransferBackend.route_proxy({}))

    def test_http_proxy_passes_its_endpoint_through(self):
        self.assertEqual(
            RustTransferBackend.route_proxy({"kind": "http_proxy", "endpoint": "http://127.0.0.1:8080"}),
            "http://127.0.0.1:8080",
        )

    def test_socks_resolves_dns_on_the_proxy(self):
        # socks5h, not socks5: resolving locally would leak the hostname past
        # the route the user chose.
        self.assertEqual(
            RustTransferBackend.route_proxy({"kind": "socks5", "endpoint": "127.0.0.1:1080"}),
            "socks5h://127.0.0.1:1080",
        )
        self.assertEqual(
            RustTransferBackend.route_proxy({"kind": "docker_socks5", "endpoint": "socks5://10.0.0.2:1080"}),
            "socks5h://10.0.0.2:1080",
        )

    def test_socks5h_endpoint_is_left_alone(self):
        self.assertEqual(
            RustTransferBackend.route_proxy({"kind": "socks5", "endpoint": "socks5h://10.0.0.2:1080"}),
            "socks5h://10.0.0.2:1080",
        )

    def test_a_proxied_route_without_an_endpoint_is_refused(self):
        # Failing here is better than silently downloading direct, which would
        # expose the address the route exists to hide.
        with self.assertRaises(RuntimeError):
            RustTransferBackend.route_proxy({"kind": "socks5", "endpoint": ""})

    def test_unknown_route_kinds_are_refused(self):
        with self.assertRaises(RuntimeError):
            RustTransferBackend.route_proxy({"kind": "carrier-pigeon", "endpoint": "nest://1"})


class RouteSelectionTests(unittest.TestCase):
    def test_selector_now_keeps_proxied_routes_on_rust(self):
        # Before proxy support every VPN or proxy route fell through to the
        # Python transport, which is the whole reason this phase exists.
        item = ResolvedItem("generic", "source", "file", size=10, direct_url="https://example.test/file")
        backend = RustTransferBackend()
        for kind in ROUTE_KINDS:
            failures = BackendSelector().select(item, {"kind": kind}, {"rust": backend})
            if not backend.available():
                self.skipTest("transfer-core binary is not built in this environment")
            self.assertTrue(failures.compatible, f"{kind}: {failures.reason}")

    def test_capabilities_advertise_every_supported_route(self):
        self.assertEqual(RustTransferBackend().capabilities().supported_route_kinds, ROUTE_KINDS)


if __name__ == "__main__":
    unittest.main()
