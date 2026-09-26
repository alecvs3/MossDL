"""WireGuard routes run as tunnels inside the Rust transfer core.

A route's .conf is kept in the secret store; when the route is used, the core
starts a userspace tunnel for it and answers with a SOCKS port on 127.0.0.1.
Everything that honours routes (transfers, crawls, resolvers, the browser)
reaches the tunnel through that port, so no WireGuard app, driver or admin
rights are needed, and several locations can be up at once.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from . import rust_session
from .telemetry import telemetry_bus
from .wireguard_conf import parse_wireguard_conf

# The first handshake is given 10 s inside the core; allow for startup too.
START_TIMEOUT = 25.0


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def core_config(conf_text: str) -> dict[str, Any]:
    """The core's tunnel settings from a .conf (validated by the shared parser)."""
    parsed = parse_wireguard_conf(conf_text)
    interface, peer = parsed["interface"], parsed["peer"]
    config: dict[str, Any] = {
        "private_key": interface["PrivateKey"],
        "addresses": _split(interface.get("Address")),
        "dns": _split(interface.get("DNS")),
        "peer": {
            "public_key": peer.get("PublicKey", ""),
            "preshared_key": peer.get("PresharedKey") or None,
            "endpoint": peer["Endpoint"],
        },
    }
    if interface.get("MTU", "").isdigit():
        config["mtu"] = int(interface["MTU"])
    if peer.get("PersistentKeepalive", "").isdigit():
        config["peer"]["persistent_keepalive"] = int(peer["PersistentKeepalive"])
    return config


def config_for(secret: str, profile: dict[str, Any]) -> dict[str, Any]:
    """A route's tunnel settings: from its own .conf, or, for an account-based
    provider, from the account's device key plus the location's server."""
    if secret.lstrip().startswith("{"):
        device = json.loads(secret)
        if not profile.get("peer_public_key") or not profile.get("endpoint"):
            raise RuntimeError(f"location {profile.get('id')!r} does not name its server")
        return {
            "private_key": device["private_key"],
            "addresses": list(device["addresses"]),
            "dns": list(device.get("dns") or ["10.64.0.1"]),
            "peer": {"public_key": profile["peer_public_key"], "endpoint": profile["endpoint"]},
        }
    return core_config(secret)


class TunnelSupervisor:
    def __init__(self, resolve_secret: Callable[[str], str | None]) -> None:
        self._resolve_secret = resolve_secret
        self._lock = threading.Lock()
        # route id -> (core process generation, "127.0.0.1:port")
        self._running: dict[str, tuple[int, str]] = {}

    @staticmethod
    def uses_core(profile: dict[str, Any]) -> bool:
        """WireGuard routes imported with their keys run in the core; older ones
        (endpoint only) still expect the WireGuard app to carry them."""
        return profile.get("kind") == "wireguard" and bool(profile.get("credential_ref")) \
            and (profile.get("tunnel") or "core") == "core"

    def ensure(self, profile: dict[str, Any]) -> str:
        """The route's SOCKS address, starting its tunnel if needed."""
        route_id = str(profile["id"])
        session = rust_session.session()
        with self._lock:
            cached = self._running.get(route_id)
            if cached and cached[0] == session.generation:
                return cached[1]
            secret = self._resolve_secret(str(profile["credential_ref"]))
            if not secret:
                raise RuntimeError(f"the WireGuard keys for {route_id!r} are missing from the secret store")
            result, generation = session.request(
                "tunnel_start", {"tunnel_id": route_id, "config": config_for(secret, profile)}, timeout=START_TIMEOUT)
            socks = str(result["socks"])
            self._running[route_id] = (generation, socks)
        telemetry_bus.record(
            level="INFO", subsystem="engine:wg_tunnels",
            message=f"[TUNNEL_UP] {route_id} via {result.get('endpoint')} on {socks}",
            context={"route_id": route_id, "endpoint": result.get("endpoint"), "socks": socks,
                     "handshake_age": result.get("handshake_age")},
            tier="engine",
        )
        return socks

    def stop(self, route_id: str) -> None:
        with self._lock:
            if self._running.pop(route_id, None) is None:
                return
        try:
            rust_session.session().request("tunnel_stop", {"tunnel_id": route_id}, timeout=5.0)
        except Exception as exc:  # the core may already be gone, taking the tunnel with it
            telemetry_bus.record(level="WARN", subsystem="engine:wg_tunnels",
                                 message=f"[TUNNEL_STOP] {route_id}: {exc}",
                                 context={"route_id": route_id}, tier="engine")

    def retain(self, keep: set[str]) -> None:
        """Stop every tunnel no longer needed by the active route or a running task."""
        with self._lock:
            idle = [route_id for route_id in self._running if route_id not in keep]
        for route_id in idle:
            self.stop(route_id)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if not self._running:
                return {}
        result, _ = rust_session.session().request("tunnel_status", {}, timeout=5.0)
        return dict(result or {})
