"""A WireGuard route, end to end: .conf in, tunnel in the core, traffic out.

wg-test-peer (built with the core) plays the VPN server on loopback, with a
resolver that only knows test.internal. Reaching test.internal therefore
proves the request went through the tunnel and resolved inside it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import route_http  # noqa: E402
from engine.models import ResolvedItem  # noqa: E402
from engine.route_http import RouteUnavailable  # noqa: E402
from engine.rust_backend import RustTransferBackend  # noqa: E402
from engine.rust_session import find_transfer_core  # noqa: E402
from engine.secrets import InMemorySecretBackend, SecretManager  # noqa: E402
from engine.service import EngineService  # noqa: E402


def _peer_binary() -> Path | None:
    name = "wg-test-peer.exe" if os.name == "nt" else "wg-test-peer"
    for profile in ("release", "debug"):
        path = ROOT / "src-tauri" / "target" / profile / name
        if path.is_file():
            return path
    return None


@unittest.skipIf(find_transfer_core() is None or _peer_binary() is None,
                 "transfer-core and wg-test-peer are not built")
class WireGuardTunnelTests(unittest.TestCase):
    def setUp(self):
        self.peer = subprocess.Popen([str(_peer_binary())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(self.peer.wait, 5)
        self.addCleanup(self.peer.stdin.close)
        self.addCleanup(self.peer.stdout.close)
        self.conf = json.loads(self.peer.stdout.readline())["conf"]
        data_dir = TemporaryDirectory()
        self.addCleanup(data_dir.cleanup)
        self.service = EngineService(data_dir.name)
        self.service.secrets = SecretManager(InMemorySecretBackend())
        self.addCleanup(self.service.close)
        self.addCleanup(route_http.set_active_route, "direct", None)

    def _activate(self, route_id: str) -> None:
        self.service.dispatch("ui_settings_update", {"settings": {"routes": {"activeLocation": route_id}}})

    def _wait_connected(self) -> None:
        deadline = time.monotonic() + 20
        while True:
            try:
                route_http.active_proxy()
                return
            except RouteUnavailable as exc:
                if "connecting" not in str(exc) or time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    def test_route_runs_in_the_core_and_carries_everything(self):
        saved = self.service.dispatch("route_save_wireguard", {"id": "wg-loop", "region": "Loopback", "conf_text": self.conf})
        self.assertEqual(saved["tunnel"], "core")
        self.assertTrue(saved["credential_ref"])
        self.assertNotIn("PrivateKey", json.dumps(self.service.dispatch("route_list", {})),
                         "the private key belongs in the secret store, not the profile")

        self._activate("wg-loop")
        self._wait_connected()
        # Engine requests (crawls, resolvers) go through the tunnel.
        with route_http.urlopen("http://test.internal/small", timeout=10) as response:
            self.assertEqual(response.read(), b"hello")
        status = self.service.dispatch("tunnel_status", {})
        self.assertIsNotNone(status["wg-loop"]["handshake_age"])

        # So do transfers on the core itself, segmented.
        with TemporaryDirectory() as root:
            item = ResolvedItem("generic", "http://test.internal/bulk", "bulk.bin", size=32 * 1024 * 1024,
                                direct_url="http://test.internal/bulk")
            path = RustTransferBackend(min_segment_size=4 * 1024 * 1024).download(
                item, root, route_profile=self.service._usable_route_profile("wg-loop"))
            self.assertEqual(Path(path).stat().st_size, 32 * 1024 * 1024)

        # Leaving the route closes its tunnel.
        self._activate("direct")
        self.assertNotIn("wg-loop", self.service.dispatch("tunnel_status", {}))

    def test_an_account_location_runs_from_the_device_key(self):
        # Account-based providers keep one device key; each location names its server.
        from engine.wireguard_conf import parse_wireguard_conf
        parsed = parse_wireguard_conf(self.conf)
        device = {"private_key": parsed["interface"]["PrivateKey"], "addresses": ["10.0.0.2/32"], "dns": ["10.0.0.1"]}
        ref = self.service.secrets.put(json.dumps(device), kind="wireguard_device")
        self.service.dispatch("route_save", {"id": "acct-loop", "kind": "wireguard", "tunnel": "core", "credential_ref": ref,
                                             "endpoint": parsed["endpoint"], "peer_public_key": parsed["peer"]["PublicKey"]})
        self._activate("acct-loop")
        self._wait_connected()
        with route_http.urlopen("http://test.internal/small", timeout=10) as response:
            self.assertEqual(response.read(), b"hello")

    def test_deleting_the_route_removes_its_keys(self):
        saved = self.service.dispatch("route_save_wireguard", {"id": "wg-loop", "conf_text": self.conf})
        self.service.dispatch("route_delete", {"profile_id": "wg-loop"})
        self.assertIsNone(self.service.secrets.resolve_operation(saved["credential_ref"]))


if __name__ == "__main__":
    unittest.main()
