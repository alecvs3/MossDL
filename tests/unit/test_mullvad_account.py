"""Mullvad by account number, against a stand-in for Mullvad's API."""
from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine import mullvad  # noqa: E402
from engine.secrets import InMemorySecretBackend, SecretManager  # noqa: E402
from engine.service import EngineService  # noqa: E402
from engine.wg_tunnels import config_for  # noqa: E402

KEY = "E75P6uryBMf9M1j8nAByGIHRdCeBKCJ+xnTzf3/pe20="
RELAYS = {
    "locations": {"se-sto": {"country": "Sweden", "city": "Stockholm"}, "ch-zrh": {"country": "Switzerland", "city": "Zurich"}},
    "wireguard": {"relays": [
        {"hostname": "se-sto-wg-001", "location": "se-sto", "active": True, "weight": 100, "public_key": KEY, "ipv4_addr_in": "185.1.1.1"},
        {"hostname": "se-sto-wg-002", "location": "se-sto", "active": True, "weight": 300, "public_key": KEY, "ipv4_addr_in": "185.1.1.2"},
        {"hostname": "ch-zrh-wg-001", "location": "ch-zrh", "active": False, "weight": 900, "public_key": KEY, "ipv4_addr_in": "185.2.2.1"},
        {"hostname": "ch-zrh-wg-002", "location": "ch-zrh", "active": True, "weight": 10, "public_key": KEY, "ipv4_addr_in": "185.2.2.2"},
    ]},
}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class FakeMullvad:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request, timeout):
        path = request.full_url.removeprefix(mullvad.API)
        self.calls.append((request.get_method(), path))
        if path == "/auth/v1/token":
            if json.loads(request.data)["account_number"] != "1234567890123456":
                raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(b'{"error":"Bad account number"}'))
            return _Response(b'{"access_token": "t"}')
        if path == "/accounts/v1/devices":
            assert request.headers["Authorization"] == "Bearer t"
            return _Response(json.dumps({"id": "dev-1", "name": "brave otter", "ipv4_address": "10.64.1.2/32",
                                         "ipv6_address": "fc00:bbbb::2/128"}).encode())
        if path == "/accounts/v1/devices/dev-1" and request.get_method() == "DELETE":
            return _Response(b"")
        if path == "/public/relays/wireguard/v2":
            return _Response(json.dumps(RELAYS).encode())
        raise AssertionError(f"unexpected call {path}")


class MullvadTests(unittest.TestCase):
    def test_one_location_per_city_on_its_best_active_server(self):
        routes = {r["id"]: r for r in mullvad.city_routes(RELAYS, "ref")}
        self.assertEqual(set(routes), {"wg-mullvad-se-sto", "wg-mullvad-ch-zrh"})
        self.assertEqual(routes["wg-mullvad-se-sto"]["endpoint"], "185.1.1.2:51820")
        self.assertEqual(routes["wg-mullvad-ch-zrh"]["endpoint"], "185.2.2.2:51820", "an inactive server is never picked")
        self.assertEqual(routes["wg-mullvad-se-sto"]["region"], "Sweden · Stockholm")

    def test_keys_are_valid_wireguard_keys(self):
        private, public = mullvad.keypair()
        import base64
        self.assertEqual(len(base64.b64decode(private)), 32)
        self.assertEqual(len(base64.b64decode(public)), 32)

    def test_device_key_plus_location_make_a_tunnel(self):
        device = {"private_key": KEY, "addresses": ["10.64.1.2/32"], "dns": ["10.64.0.1"]}
        config = config_for(json.dumps(device), {"id": "wg-mullvad-se-sto", "endpoint": "185.1.1.2:51820", "peer_public_key": KEY})
        self.assertEqual(config["peer"], {"public_key": KEY, "endpoint": "185.1.1.2:51820"})
        self.assertEqual(config["dns"], ["10.64.0.1"])

    def test_sign_in_and_out(self):
        fake = FakeMullvad()
        with TemporaryDirectory() as data_dir, patch("engine.route_http.urlopen", fake):
            service = EngineService(data_dir)
            service.secrets = SecretManager(InMemorySecretBackend())
            try:
                with self.assertRaisesRegex(RuntimeError, "Bad account number"):
                    service.dispatch("provider_account_login", {"provider": "mullvad", "account_number": "0000 0000 0000 0000"})
                result = service.dispatch("provider_account_login", {"provider": "mullvad", "account_number": "1234 5678 9012 3456"})
                self.assertEqual(result, {"locations": 2, "device_name": "brave otter"})
                profiles = [p for p in service.dispatch("route_list", {}) if p["id"].startswith("wg-mullvad-")]
                self.assertEqual(len(profiles), 2)
                ref = profiles[0]["credential_ref"]
                self.assertNotIn("private_key", json.dumps(profiles), "keys stay in the secret store")

                # Removing one city keeps the key the other still uses.
                service.dispatch("route_delete", {"profile_id": "wg-mullvad-ch-zrh"})
                self.assertIsNotNone(service.secrets.resolve_operation(ref))

                service.dispatch("provider_account_logout", {"provider": "mullvad"})
                self.assertIn(("DELETE", "/accounts/v1/devices/dev-1"), fake.calls, "the device slot is freed")
                self.assertFalse([p for p in service.dispatch("route_list", {}) if p["id"].startswith("wg-mullvad-")])
                self.assertIsNone(service.secrets.resolve_operation(ref))
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
