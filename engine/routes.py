import json
import os
import re
import socket
import sys
import time
from dataclasses import asdict
from typing import Any, Callable
from urllib.parse import urlsplit

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

from .models import RouteHealth, RouteProfile
from .telemetry import telemetry_bus
from .fallback import route_error_is_switchable
from . import route_http


SUPPORTED_KINDS = {"direct", "http_proxy", "socks5", "system_vpn", "docker_socks5", "wireguard"}

_ADAPTER_CACHE_TTL = 15.0
_cached_adapters: list[dict[str, Any]] = []
_adapter_cache_timestamp: float = 0.0

VPN_INTERFACE_PATTERN = re.compile(
    r"tunnel|wintun|wireguard|tap|vpn|tailscale|proton|mullvad|nord|express|cloudflare|warp|cyberghost|ipvanish",
    re.IGNORECASE,
)


class RouteManager:
    """Provider-neutral route profiles; Docker lifecycle stays user-owned."""

    def __init__(self, store):
        self.store = store
        self._health_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # Set by the service: starts a core tunnel for a profile, returns its SOCKS address.
        self.tunnel_address: Callable[[dict[str, Any]], str] | None = None
        if not self.store.list_route_profiles():
            self.save(RouteProfile("direct"))

    @staticmethod
    def parse_wireguard_conf(conf_text: str) -> dict[str, Any]:
        """Parses a standard WireGuard .conf profile (Mullvad, ProtonVPN, NordVPN, etc.)."""
        from .wireguard_conf import parse_wireguard_conf
        return parse_wireguard_conf(conf_text)

    @staticmethod
    def validate(profile: dict[str, Any]) -> RouteProfile:
        profile_id = str(profile.get("id", "")).strip()
        kind = str(profile.get("kind", "direct")).strip().lower()
        if not profile_id:
            raise ValueError("route profile id is required")
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"unsupported route kind: {kind}")
        endpoint = profile.get("endpoint")
        if kind in {"http_proxy", "socks5", "docker_socks5"}:
            if not endpoint:
                raise ValueError("proxy route requires an endpoint")
            parsed = urlsplit(str(endpoint))
            expected = "http" if kind == "http_proxy" else "socks5"
            if parsed.scheme != expected or not parsed.hostname or not parsed.port:
                raise ValueError(f"endpoint must be a {expected}://host:port URL")
            if parsed.username or parsed.password:
                raise ValueError("route credentials must use credential_ref, not endpoint userinfo")
        tunnel = profile.get("tunnel")
        if tunnel is not None and (kind != "wireguard" or tunnel not in {"core", "system"}):
            raise ValueError("tunnel is 'core' or 'system', and only for WireGuard routes")
        return RouteProfile(profile_id, kind, str(endpoint) if endpoint else None,
                            profile.get("credential_ref"), profile.get("region"),
                            profile.get("healthcheck_url"), bool(profile.get("enabled", True)), tunnel,
                            profile.get("peer_public_key"))

    def save(self, profile: RouteProfile | dict[str, Any]) -> dict[str, Any]:
        value = asdict(profile) if isinstance(profile, RouteProfile) else profile
        validated = self.validate(value)
        self.store.save_route_profile(asdict(validated))
        return validated.to_dict()

    def profiles(self) -> list[dict[str, Any]]:
        return self.store.list_route_profiles()

    def delete(self, profile_id: str) -> dict[str, Any]:
        if profile_id == "direct":
            raise ValueError("cannot delete direct route")
        self.store.delete_route_profile(profile_id)
        self._health_cache.pop(profile_id, None)
        return {"deleted": profile_id}

    def health(self, profile_id: str, force: bool = False) -> dict[str, Any]:
        value = next((item for item in self.profiles() if item["id"] == profile_id), None)
        if value is None:
            raise KeyError("route profile not found")
        profile = self.validate(value)
        now = time.time()
        if not force and profile_id in self._health_cache:
            cached_at, cached_res = self._health_cache[profile_id]
            if now - cached_at < 15.0:
                return dict(cached_res)

        if profile.kind == "direct":
            try:
                import httpx
                target = profile.healthcheck_url or "https://api.ipify.org?format=json"
                with httpx.Client(timeout=3.0, follow_redirects=True) as client:
                    response = client.get(target)
                    response.raise_for_status()
                    public_ip = response.json().get("ip") if "json" in response.headers.get("content-type", "") else None
                res = RouteHealth(profile.id, True, now, public_ip).to_dict()
                self._health_cache[profile_id] = (now, res)
                return res
            except Exception as exc:
                res = RouteHealth(profile.id, False, now, error=f"Host internet unreachable: {exc}").to_dict()
                self._health_cache[profile_id] = (now, res)
                return res

        if profile.kind == "wireguard" and self.tunnel_address is not None and profile.credential_ref                 and (profile.tunnel or "core") == "core":
            # A tunnel in the core: bring it up and ask what address the world sees.
            try:
                socks = self.tunnel_address(value)
                target = profile.healthcheck_url or "https://api.ipify.org?format=json"
                with route_http.urlopen(target, timeout=8.0, proxy=f"socks5h://{socks}") as response:
                    body = response.read(4096).decode("utf-8", "replace")
                public_ip = json.loads(body).get("ip") if body.lstrip().startswith("{") else body.strip() or None
                res = RouteHealth(profile.id, True, now, public_ip).to_dict()
            except Exception as exc:
                res = RouteHealth(profile.id, False, now, error=f"Tunnel unavailable: {exc}").to_dict()
            self._health_cache[profile_id] = (now, res)
            return res

        if profile.kind in {"system_vpn", "wireguard"}:
            adapters = detect_system_vpn_adapters(force_refresh=force)
            up_adapters = [a for a in adapters if a.get("status") == "Up"]

            GENERIC_TOKENS = {
                "vpn", "tunnel", "tap", "tun", "wireguard", "wintun", "wg", "adapter",
                "network", "connection", "client", "profile", "route", "sec", "safe",
                "fast", "default", "custom"
            }
            raw_tokens = [k for k in re.split(r"[-_ ]+", profile.id.lower()) if len(k) >= 3]
            specific_tokens = [k for k in raw_tokens if k not in GENERIC_TOKENS]

            if specific_tokens:
                # Must match at least one provider/profile-specific token
                matching = [
                    a for a in up_adapters
                    if any(t in a.get("name", "").lower() or t in a.get("description", "").lower() for t in specific_tokens)
                ]
            elif profile.kind == "wireguard":
                # Pure WireGuard profile without provider token (e.g. id "wireguard")
                matching = [
                    a for a in up_adapters
                    if any(t in a.get("name", "").lower() or t in a.get("description", "").lower() for t in ("wireguard", "wintun"))
                ]
            elif profile.id in {"system_vpn", "vpn"}:
                # Pure generic VPN profile
                matching = up_adapters
            else:
                matching = []

            if not matching:
                err_msg = (
                    f"WireGuard tunnel '{profile.id}' is not active on Windows. Start the tunnel in your WireGuard app."
                    if profile.kind == "wireguard"
                    else f"System VPN '{profile.id}' adapter is disconnected or not installed on Windows. Please connect in your VPN app."
                )
                res = RouteHealth(profile.id, False, now, error=err_msg).to_dict()
                self._health_cache[profile_id] = (now, res)
                return res

            try:
                import httpx
                target = profile.healthcheck_url or "https://api.ipify.org?format=json"
                with httpx.Client(timeout=4.0, follow_redirects=True) as client:
                    response = client.get(target)
                    response.raise_for_status()
                    public_ip = response.json().get("ip") if "json" in response.headers.get("content-type", "") else None
                res = RouteHealth(profile.id, True, now, public_ip).to_dict()
                self._health_cache[profile_id] = (now, res)
                return res
            except Exception as exc:
                res = RouteHealth(profile.id, False, now, error=f"VPN adapter active but internet check failed: {exc}").to_dict()
                self._health_cache[profile_id] = (now, res)
                return res

        if profile.endpoint:
            parsed = urlsplit(profile.endpoint)
            # Fast TCP socket pre-check with 1.2s timeout
            if parsed.hostname and parsed.port:
                try:
                    with socket.create_connection((parsed.hostname, parsed.port), timeout=1.2):
                        pass
                except (ConnectionRefusedError, socket.timeout, OSError) as sock_err:
                    diag = f"Endpoint unreachable on {parsed.hostname}:{parsed.port} ({sock_err}). Verify proxy is running."
                    res = RouteHealth(profile.id, False, now, error=diag).to_dict()
                    self._health_cache[profile_id] = (now, res)
                    return res

        try:
            import httpx
            target = profile.healthcheck_url or "https://api.ipify.org?format=json"
            with httpx.Client(proxy=profile.endpoint, timeout=3.5, follow_redirects=True) as client:
                response = client.get(target)
                response.raise_for_status()
                public_ip = response.json().get("ip") if "json" in response.headers.get("content-type", "") else None
            res = RouteHealth(profile.id, True, now, public_ip).to_dict()
            self._health_cache[profile_id] = (now, res)
            return res
        except Exception as exc:
            res = RouteHealth(profile.id, False, now, error=str(exc)).to_dict()
            self._health_cache[profile_id] = (now, res)
            return res

    def record_attempt(self, task_id: str, profile_id: str, outcome: str, reason: str | None = None) -> dict[str, Any]:
        self.store.record_route_attempt(task_id, profile_id, outcome, reason)
        return {"task_id": task_id, "profile_id": profile_id, "outcome": outcome, "reason": reason}

    def next_profile(self, task_id: str, candidates: list[str]) -> str | None:
        tried = {row["profile_id"] for row in self.store.list_route_attempts(task_id)}
        return next((profile_id for profile_id in candidates if profile_id not in tried), None)

    def next_healthy_route(
        self,
        task_id: str,
        current_route_id: str | None = None,
        exclude_routes: set[str] | None = None,
        *,
        include_proxies: bool = True,
        allow_direct: bool = True,
    ) -> RouteProfile | None:
        """
        Finds the next best available and healthy route profile from the configured pool.
        Prioritizes healthy VPN routes (WireGuard, Docker SOCKS5, System VPN) over proxies,
        and direct last. Every skipped candidate is logged with the reason.
        """
        excludes = set(exclude_routes or set())
        if current_route_id:
            excludes.add(current_route_id)

        # Retrieve route attempts for this task to avoid cycling failed routes
        tried_routes = {
            row["profile_id"] for row in self.store.list_route_attempts(task_id)
            if row.get("outcome") in {"failed", "unreachable", "blocked", "quota_exceeded"}
        }

        all_profiles = [self.validate(p) for p in self.profiles() if p.get("enabled", True)]

        def _route_sort_key(p: RouteProfile) -> int:
            if p.kind in {"wireguard", "docker_socks5", "socks5", "system_vpn"}:
                return 1
            if p.kind == "http_proxy":
                return 2
            return 3  # direct

        def _skip(candidate: RouteProfile, reason: str, **extra: Any) -> None:
            telemetry_bus.record(
                level="DEBUG", subsystem="engine:routes", tier="engine",
                message=f"route candidate {candidate.id} skipped: {reason}",
                context={"task_id": task_id, "route": candidate.id, "kind": candidate.kind, "reason": reason, **extra},
            )

        for candidate in sorted(all_profiles, key=_route_sort_key):
            if candidate.id in excludes:
                _skip(candidate, "current_or_excluded")
                continue
            if candidate.id in tried_routes:
                _skip(candidate, "already_failed_for_task")
                continue
            if not include_proxies and candidate.kind in {"http_proxy", "socks5", "docker_socks5"}:
                _skip(candidate, "proxies_disabled_by_setting")
                continue
            if not allow_direct and candidate.kind == "direct":
                _skip(candidate, "direct_fallback_disabled_by_setting")
                continue
            try:
                h = self.health(candidate.id)
            except Exception as exc:  # a broken probe must not stop failover; it is logged, not swallowed
                _skip(candidate, "health_check_error", error=repr(exc))
                continue
            if not h.get("healthy"):
                _skip(candidate, "unhealthy", error=h.get("error"))
                continue
            return candidate

        return None

    @staticmethod
    def automatic_switch_allowed(error: str = "", status_code: int | None = None,
                                 category: str | None = None) -> bool:
        return route_error_is_switchable(error, status_code, category)

    def test_proxy_download(self, endpoint: str, kind: str = "http_proxy", timeout: float = 6.0) -> dict[str, Any]:
        return test_proxy_download(endpoint, kind, timeout=timeout)


def _detect_windows_adapters() -> list[dict[str, Any]]:
    try:
        iphlpapi = ctypes.windll.iphlpapi
    except Exception:
        return []

    AF_UNSPEC = 0
    flags = 0x0001 | 0x0002 | 0x0004 | 0x0008  # skip unicast, anycast, multicast, dns
    IfOperStatusUp = 1

    class IP_ADAPTER_ADDRESSES(ctypes.Structure):
        pass

    IP_ADAPTER_ADDRESSES._fields_ = [
        ("Length", wintypes.ULONG),
        ("IfIndex", wintypes.DWORD),
        ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.c_void_p),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", wintypes.LPWSTR),
        ("Description", wintypes.LPWSTR),
        ("FriendlyName", wintypes.LPWSTR),
        ("PhysicalAddress", wintypes.BYTE * 8),
        ("PhysicalAddressLength", wintypes.DWORD),
        ("Flags", wintypes.DWORD),
        ("Mtu", wintypes.DWORD),
        ("IfType", wintypes.DWORD),
        ("OperStatus", wintypes.DWORD),
    ]

    buf_len = wintypes.ULONG(16384)
    buf = ctypes.create_string_buffer(buf_len.value)

    ret = iphlpapi.GetAdaptersAddresses(AF_UNSPEC, flags, None, buf, ctypes.byref(buf_len))
    if ret == 111:  # ERROR_BUFFER_OVERFLOW
        buf = ctypes.create_string_buffer(buf_len.value)
        ret = iphlpapi.GetAdaptersAddresses(AF_UNSPEC, flags, None, buf, ctypes.byref(buf_len))

    if ret != 0:
        return []

    adapters: list[dict[str, Any]] = []
    curr = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    while curr:
        addr = curr.contents
        friendly_name = addr.FriendlyName or ""
        description = addr.Description or ""
        status = "Up" if addr.OperStatus == IfOperStatusUp else "Down"

        if status == "Up" and (VPN_INTERFACE_PATTERN.search(friendly_name) or VPN_INTERFACE_PATTERN.search(description)):
            adapters.append({
                "name": friendly_name,
                "description": description,
                "status": status,
            })
        curr = addr.Next

    return adapters


def detect_system_vpn_adapters(force_refresh: bool = False) -> list[dict[str, Any]]:
    global _cached_adapters, _adapter_cache_timestamp
    now = time.time()
    if not force_refresh and (now - _adapter_cache_timestamp) < _ADAPTER_CACHE_TTL:
        return [dict(a) for a in _cached_adapters]

    adapters: list[dict[str, Any]] = []
    if sys.platform == "win32":
        adapters = _detect_windows_adapters()
    elif sys.platform == "linux":
        try:
            sys_class_net = "/sys/class/net"
            if os.path.exists(sys_class_net):
                for iface in os.listdir(sys_class_net):
                    oper_file = os.path.join(sys_class_net, iface, "operstate")
                    status = "unknown"
                    if os.path.exists(oper_file):
                        with open(oper_file, "r") as f:
                            status = "Up" if f.read().strip().lower() == "up" else "Down"
                    if status == "Up" and VPN_INTERFACE_PATTERN.search(iface):
                        adapters.append({
                            "name": iface,
                            "description": f"Linux Virtual Interface ({iface})",
                            "status": status,
                        })
        except Exception:
            pass

    _cached_adapters = adapters
    _adapter_cache_timestamp = now
    return [dict(a) for a in adapters]


_PUBLIC_PROXIES_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])


def fetch_public_proxies(limit: int = 50, protocol: str | None = None) -> list[dict[str, Any]]:
    """Fetches fast free proxies from the GeoNode public API with memory caching."""
    global _PUBLIC_PROXIES_CACHE
    now = time.time()
    if now - _PUBLIC_PROXIES_CACHE[0] < 60.0 and _PUBLIC_PROXIES_CACHE[1]:
        proxies = _PUBLIC_PROXIES_CACHE[1]
    else:
        import json
        import urllib.request
        count = max(10, min(100, int(limit or 50)))
        url = f"https://proxylist.geonode.com/api/proxy-list?page=1&limit={count}&sort_by=responseTime&sort_type=asc"
        req = urllib.request.Request(url, headers={"User-Agent": "Transfer-Manager/0.1.0"})
        try:
            with route_http.urlopen(req, timeout=8.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            raw = data.get("data", [])
            proxies = []
            for item in raw:
                proxies.append({
                    "ip": str(item.get("ip", "")),
                    "port": str(item.get("port", "")),
                    "protocols": list(item.get("protocols", [])),
                    "country": str(item.get("country", "")),
                    "city": str(item.get("city", "") or ""),
                    "responseTime": int(item.get("responseTime", 0) or item.get("latency", 0) or 0),
                    "anonymityLevel": str(item.get("anonymityLevel", "") or "elite"),
                    "upTime": round(float(item.get("upTime", 0) or 0), 1),
                })
            _PUBLIC_PROXIES_CACHE = (now, proxies)
        except Exception:
            proxies = _PUBLIC_PROXIES_CACHE[1]

    if protocol:
        p_lower = protocol.lower()
        return [p for p in proxies if p_lower in [x.lower() for x in p["protocols"]]]
    return proxies


def with_proxy_credentials(endpoint: str, secret: str | None) -> str:
    """Returns the endpoint with ``user:password`` userinfo for one call.

    Route profiles store only a credential_ref; the resolved secret is added
    here at use time and must never be persisted or returned to the UI.
    """
    if not secret or not endpoint or ":" not in secret:
        return endpoint
    from urllib.parse import quote
    user, password = secret.split(":", 1)
    parts = urlsplit(endpoint if "://" in endpoint else f"http://{endpoint}")
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}" + (f":{parts.port}" if parts.port else "")
    return parts._replace(netloc=netloc).geturl()


def test_proxy_download(endpoint: str, kind: str = "http_proxy", timeout: float = 6.0) -> dict[str, Any]:
    """Tests an actual live HTTP download through the specified proxy."""
    import time
    t0 = time.perf_counter()
    endpoint = endpoint.strip()
    if "://" not in endpoint:
        endpoint = f"http://{endpoint}" if kind == "http_proxy" else f"socks5://{endpoint}"

    try:
        import httpx
        with httpx.Client(proxy=endpoint, timeout=timeout, follow_redirects=True) as client:
            resp = client.get("http://httpbin.org/ip")
            elapsed_ms = round((time.perf_counter() - t0) * 1000)
            resp.raise_for_status()
            data = resp.json() if "json" in resp.headers.get("content-type", "") else {}
            egress = data.get("origin") or resp.text.strip()[:64]
            return {
                "ok": True,
                "latency_ms": elapsed_ms,
                "egress_ip": egress,
                "status_code": resp.status_code,
                "bytes_received": len(resp.content),
            }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        return {
            "ok": False,
            "latency_ms": elapsed_ms,
            "error": str(exc),
        }


