"""Mullvad by account number: every Mullvad city becomes a location.

The app makes its own WireGuard key, registers it on the account as a device
(Mullvad allows five), and builds one location per city from Mullvad's public
relay list. The key works on every server, so switching city swaps the peer
without new configs. Only the account number, the device and its key are kept,
in the secret store.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

from . import route_http

API = "https://api.mullvad.net"
PROVIDER_ID = "mullvad"


def keypair() -> tuple[str, str]:
    """A fresh WireGuard key pair, base64 (private, public)."""
    from Crypto.PublicKey import ECC
    key = ECC.generate(curve="Curve25519")
    return (base64.b64encode(key.seed).decode(),
            base64.b64encode(key.public_key().export_key(format="raw")).decode())


def _call(method: str, path: str, body: dict[str, Any] | None = None, token: str | None = None) -> Any:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API}{path}", method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
    try:
        with route_http.urlopen(request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error") or detail
        except ValueError:
            pass
        raise RuntimeError(f"Mullvad: {detail} (HTTP {exc.code})") from None
    return json.loads(raw) if raw else None


def _token(account_number: str) -> str:
    number = "".join(ch for ch in account_number if ch.isdigit())
    if len(number) != 16:
        raise ValueError("a Mullvad account number is 16 digits")
    return _call("POST", "/auth/v1/token", {"account_number": number})["access_token"]


def register(account_number: str) -> dict[str, Any]:
    """Add this app as a device on the account; the result is kept as a secret."""
    private_key, public_key = keypair()
    device = _call("POST", "/accounts/v1/devices", {"pubkey": public_key, "hijack_dns": False},
                   token=_token(account_number))
    return {
        "account_number": "".join(ch for ch in account_number if ch.isdigit()),
        "device_id": device["id"],
        "device_name": device.get("name"),
        "private_key": private_key,
        "addresses": [a for a in (device.get("ipv4_address"), device.get("ipv6_address")) if a],
        # Mullvad's resolver inside the tunnel.
        "dns": ["10.64.0.1"],
    }


def unregister(device: dict[str, Any]) -> None:
    """Free the device slot on the account."""
    _call("DELETE", f"/accounts/v1/devices/{device['device_id']}", token=_token(device["account_number"]))


def city_routes(relays: dict[str, Any], credential_ref: str) -> list[dict[str, Any]]:
    """One location per city, on the city's best active server."""
    locations = relays.get("locations", {})
    best: dict[str, dict[str, Any]] = {}
    for relay in relays.get("wireguard", {}).get("relays", []):
        if not relay.get("active") or not relay.get("public_key") or not relay.get("ipv4_addr_in"):
            continue
        current = best.get(relay["location"])
        if current is None or relay.get("weight", 0) > current.get("weight", 0):
            best[relay["location"]] = relay
    routes = []
    for code, relay in sorted(best.items()):
        place = locations.get(code, {})
        routes.append({
            # The UI groups locations by their wg-<provider>- prefix.
            "id": f"wg-{PROVIDER_ID}-{code}",
            "kind": "wireguard",
            "tunnel": "core",
            "endpoint": f"{relay['ipv4_addr_in']}:51820",
            "region": f"{place.get('country', code)} · {place.get('city', '')}".strip(" ·"),
            "credential_ref": credential_ref,
            "peer_public_key": relay["public_key"],
            "enabled": True,
        })
    return routes


def relay_list() -> dict[str, Any]:
    return _call("GET", "/public/relays/wireguard/v2")

