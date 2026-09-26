"""WireGuard .conf parsing that accepts every shape providers actually export.

Follows wg-quick semantics: case-insensitive keys, ``#`` comments anywhere on a
line, repeated list keys (Address/DNS/AllowedIPs) are merged, and every
``[Peer]`` section is kept.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

_CANONICAL = {k.lower(): k for k in (
    "PrivateKey", "Address", "DNS", "MTU", "ListenPort", "Table", "FwMark",
    "PreUp", "PostUp", "PreDown", "PostDown", "SaveConfig",
    "PublicKey", "PresharedKey", "Endpoint", "AllowedIPs", "PersistentKeepalive",
)}
_LIST_KEYS = {"Address", "DNS", "AllowedIPs"}
_PLACEHOLDER_TOKENS = ("<", ">", "your-", "placeholder")


def _is_wg_key(value: str) -> bool:
    """A WireGuard key is 32 bytes, base64-encoded (44 chars ending in '=')."""
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


def _parse_sections(conf_text: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    interface: dict[str, str] = {}
    peers: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    # Notepad saves UTF-8 with a BOM; ';' only starts a comment at line start (PostUp may contain ';').
    for raw in conf_text.lstrip("\ufeff").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section == "interface":
                current = interface
            elif section == "peer":
                current = {}
                peers.append(current)
            else:
                current = None
            continue
        if "=" not in line or current is None:
            continue
        key, val = (part.strip() for part in line.split("=", 1))
        key = _CANONICAL.get(key.lower(), key)
        if key in _LIST_KEYS and current.get(key):
            current[key] = f"{current[key]}, {val}"
        else:
            current[key] = val
    return interface, peers


def parse_wireguard_conf(conf_text: str) -> dict[str, Any]:
    interface, peers = _parse_sections(conf_text)

    priv_key = interface.get("PrivateKey", "")
    if not priv_key:
        raise ValueError("WireGuard configuration missing PrivateKey in [Interface]")
    if any(token in priv_key.lower() for token in _PLACEHOLDER_TOKENS):
        raise ValueError("WireGuard PrivateKey is a placeholder. Please replace it with your actual WireGuard private key from your VPN provider.")
    if not _is_wg_key(priv_key):
        raise ValueError("WireGuard PrivateKey appears invalid. Must be a 44-character base64 key.")

    if not peers:
        raise ValueError("WireGuard configuration missing [Peer] section")
    for index, peer in enumerate(peers, start=1):
        if not peer.get("Endpoint"):
            raise ValueError(f"WireGuard configuration missing Endpoint in [Peer] #{index}")
        public_key = peer.get("PublicKey")
        if public_key and not _is_wg_key(public_key):
            raise ValueError(f"WireGuard PublicKey in [Peer] #{index} appears invalid. Must be a 44-character base64 key.")

    first = peers[0]
    return {
        "interface": interface,
        "peer": first,
        "endpoint": first["Endpoint"],
        "address": interface.get("Address"),
        "dns": interface.get("DNS"),
        # Appended fields (contract is append-only).
        "peers": peers,
        "allowed_ips": first.get("AllowedIPs"),
    }
