"""Every engine request that is not itself a transfer goes out on the active route.

Transfers carry their own route profile. Everything else the engine fetches
(page crawls, favicons, shortlink and host resolvers, preflights) used to open
sockets directly, so a user with a VPN or proxy route selected still showed
their real address to every site they explored. Those requests go through
here instead, and when the route cannot be honoured they fail rather than
quietly going direct.
"""
from __future__ import annotations

import contextvars
import threading
import urllib.parse
import urllib.request
from typing import Any

from .telemetry import telemetry_bus

# Routes the operating system already carries: the kernel sends the socket the
# right way, and a proxy would be a second, wrong hop.
KERNEL_ROUTED_KINDS = {"direct", "system_vpn", "wireguard"}


class RouteUnavailable(RuntimeError):
    """The selected route cannot carry this request; sending it direct would leak."""


def proxy_url_for(route_profile: dict | None) -> str | None:
    """The proxy URL a route profile implies, or None when the kernel routes it."""
    if not route_profile:
        return None
    kind = str(route_profile.get("kind", "direct") or "direct").lower()
    if kind == "wireguard" and route_profile.get("local_proxy"):
        # A tunnel run by the transfer core: a local SOCKS port, DNS inside the tunnel.
        return f"socks5h://{route_profile['local_proxy']}"
    if kind in KERNEL_ROUTED_KINDS:
        return None
    endpoint = str(route_profile.get("endpoint") or "").strip()
    if not endpoint:
        raise RouteUnavailable(f"route kind {kind!r} has no endpoint to proxy through")
    if kind == "http_proxy":
        return endpoint if "://" in endpoint else f"http://{endpoint}"
    if kind in {"socks5", "docker_socks5"}:
        # socks5h keeps DNS on the proxy side; resolving locally would leak
        # the hostname past the route the user picked.
        if "://" not in endpoint:
            return f"socks5h://{endpoint}"
        return endpoint.replace("socks5://", "socks5h://", 1)
    raise RouteUnavailable(f"route kind {kind!r} is not supported for proxied requests")


class _ActiveRoute:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.profile_id = "direct"
        self.proxy: str | None = None
        self.error: str | None = None


_active = _ActiveRoute()
# Sentinel: "use whatever route is active", as opposed to an explicit proxy or None.
ACTIVE: Any = object()


def set_active_route(profile_id: str, proxy: str | None, error: str | None = None) -> None:
    """Record the route non-transfer requests use. An error blocks them instead."""
    with _active.lock:
        _active.profile_id, _active.proxy, _active.error = profile_id, proxy, error
    telemetry_bus.record(
        level="WARN" if error else "INFO", subsystem="engine:route_http",
        message=f"[ROUTE_ACTIVE] {profile_id} " + (f"blocked: {error}" if error else ("proxied" if proxy else "kernel-routed")),
        context={"profile_id": profile_id, "proxied": bool(proxy), "error": error}, tier="engine",
    )


# A task's own route, bound for the rest of its asyncio task. asyncio.to_thread
# copies context, so provider resolution run for the task inherits it: a host
# that ties its download link to the resolving IP sees the same IP both times.
_task_route: contextvars.ContextVar[tuple[str | None] | None] = contextvars.ContextVar("task_route", default=None)


def bind_task_route(proxy: str | None) -> None:
    """Send this task's non-transfer requests (resolvers, preflights) on its route."""
    _task_route.set((proxy,))


def active_proxy() -> str | None:
    """The bound task route, else the active route; raises when that is unusable."""
    bound = _task_route.get()
    if bound is not None:
        return bound[0]
    with _active.lock:
        if _active.error:
            raise RouteUnavailable(f"route {_active.profile_id!r} is unavailable: {_active.error}")
        return _active.proxy


def _resolve(proxy: Any) -> str | None:
    return active_proxy() if proxy is ACTIVE else proxy


def _socks_handler(proxy: str) -> urllib.request.BaseHandler:
    try:
        import socks  # PySocks
        from sockshandler import SocksiPyHandler
    except ImportError as exc:  # pragma: no cover - PySocks is a declared requirement
        raise RouteUnavailable("SOCKS routes need PySocks, which is not installed") from exc
    parts = urllib.parse.urlsplit(proxy)
    kind = socks.SOCKS4 if parts.scheme.startswith("socks4") else socks.SOCKS5
    # rdns: the proxy resolves hostnames, so DNS never leaves on the local link.
    return SocksiPyHandler(kind, parts.hostname, parts.port or 1080, rdns=True,
                           username=urllib.parse.unquote(parts.username) if parts.username else None,
                           password=urllib.parse.unquote(parts.password) if parts.password else None)


def opener(*handlers: urllib.request.BaseHandler, proxy: Any = ACTIVE) -> urllib.request.OpenerDirector:
    """A urllib opener bound to the route (the active one unless `proxy` is given)."""
    url = _resolve(proxy)
    if not url:
        # An empty ProxyHandler also ignores HTTP(S)_PROXY from the environment:
        # the route the user picked is the only route.
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers)
    if url.startswith("socks"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), _socks_handler(url), *handlers)
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}), *handlers)


def urlopen(request: urllib.request.Request | str, timeout: float, *, proxy: Any = ACTIVE):
    """Drop-in for urllib.request.urlopen that stays on the route."""
    return opener(proxy=proxy).open(request, timeout=timeout)


def curl_proxies(proxy: Any = ACTIVE) -> dict[str, str] | None:
    """The same route in the form curl_cffi and requests expect."""
    url = _resolve(proxy)
    return {"http": url, "https": url} if url else None


def playwright_proxy(proxy: Any = ACTIVE) -> dict[str, str] | None:
    """The route as a Playwright browser-context proxy.

    Chromium resolves hostnames on the far side of a SOCKS5 proxy by itself, so
    socks5h becomes plain socks5 here. It cannot authenticate to a SOCKS proxy
    at all; that route is refused rather than silently bypassed.
    """
    url = _resolve(proxy)
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    scheme = "socks5" if parts.scheme.startswith("socks5") else parts.scheme
    server = f"{scheme}://{parts.hostname}:{parts.port}" if parts.port else f"{scheme}://{parts.hostname}"
    if parts.username and scheme.startswith("socks"):
        raise RouteUnavailable("the browser cannot sign in to a SOCKS proxy; use an HTTP proxy or a tunnel for browser steps")
    result = {"server": server}
    if parts.username:
        result["username"] = urllib.parse.unquote(parts.username)
        result["password"] = urllib.parse.unquote(parts.password or "")
    return result
