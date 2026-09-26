from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import hashlib
import time
import unittest
from pathlib import Path

from engine.db import TaskStore
from engine.errors import QuotaExceeded, ProviderMappedError
from engine.fallback import ProviderFallback, source_fingerprint
from engine.models import DownloadTask, ResolvedItem, RouteProfile
from engine.routes import RouteManager
from engine.service import EngineService


class VpnAndQuotaFailoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path("test_scratch_vpn_quota")
        self.temp_dir.mkdir(exist_ok=True)
        self.db_path = self.temp_dir / "test_store.db"
        self.store = TaskStore(str(self.db_path))
        self.fallback = ProviderFallback(self.store)
        self.routes = RouteManager(self.store)

    def tearDown(self) -> None:
        import shutil
        self.store.close()
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_quota_lock_and_strict_no_backtrack(self) -> None:
        task_id = "test-task-quota-1"
        self.store.save(DownloadTask(id=task_id, source_url="https://rapidgator.net/file/123/archive.rar", destination=str(self.temp_dir)))
        mirror_a = "https://rapidgator.net/file/123/archive.rar"
        mirror_b = "https://mediafire.com/file/456/archive.rar"
        mirror_c = "https://gofile.io/d/789"

        alternatives = [
            {"url": mirror_a, "provider": "rapidgator"},
            {"url": mirror_b, "provider": "mediafire"},
            {"url": mirror_c, "provider": "gofile"},
        ]

        # Initial candidate selection: all 3 mirrors are available
        cands = self.fallback.candidates(task_id, alternatives)
        self.assertEqual(len(cands), 3)
        self.assertEqual(cands[0]["url"], mirror_a)

        # 1. Simulate Mirror A hitting quota
        self.fallback.lock_quota(task_id, "rapidgator", mirror_a, error="Daily 5GB limit reached")
        self.assertTrue(self.fallback.is_quota_locked(task_id, mirror_a, "rapidgator"))

        # Next candidate query must exclude Mirror A and select Mirror B
        cands_after_a = self.fallback.candidates(task_id, alternatives)
        self.assertEqual(len(cands_after_a), 2)
        self.assertEqual(cands_after_a[0]["url"], mirror_b)
        self.assertEqual(cands_after_a[1]["url"], mirror_c)

        # 2. Simulate Mirror B hitting quota
        self.fallback.lock_quota(task_id, "mediafire", mirror_b, error="Bandwidth limit exceeded")
        cands_after_b = self.fallback.candidates(task_id, alternatives)
        self.assertEqual(len(cands_after_b), 1)
        self.assertEqual(cands_after_b[0]["url"], mirror_c)

        # 3. Simulate multiple retries: ensure neither A nor B is EVER selected again
        for _ in range(5):
            cands_retry = self.fallback.candidates(task_id, alternatives)
            self.assertEqual(len(cands_retry), 1)
            self.assertEqual(cands_retry[0]["url"], mirror_c)
            urls = [c["url"] for c in cands_retry]
            self.assertNotIn(mirror_a, urls)
            self.assertNotIn(mirror_b, urls)

    def test_route_manager_next_healthy_route_pool(self) -> None:
        # Configure multiple routes in RouteManager
        # 1. Direct (default)
        # 2. WireGuard profile
        self.routes.save({
            "id": "wg-us-mullvad",
            "kind": "wireguard",
            "endpoint": "198.51.100.1:51820",
            "enabled": True,
        })
        self.routes.save({
            "id": "wg-eu-proton",
            "kind": "wireguard",
            "endpoint": "198.51.100.2:51820",
            "enabled": True,
        })

        task_id = "task-vpn-rot-1"
        self.store.save(DownloadTask(id=task_id, source_url="https://example.com/test.bin", destination=str(self.temp_dir)))

        # Simulate healthy VPN tunnels to verify route rotation and priority ordering
        self.routes.health = lambda pid, force=False: {"healthy": True, "public_ip": "198.51.100.1"}

        # Next route from direct should pick the highest-priority healthy VPN profile
        next_route = self.routes.next_healthy_route(task_id, current_route_id="direct")
        self.assertIsNotNone(next_route)
        self.assertIn(next_route.id, {"wg-us-mullvad", "wg-eu-proton"})

        # If wg-us-mullvad fails, record failure
        self.routes.record_attempt(task_id, next_route.id, "failed", "Connection timed out")

        # Next rotation should skip the failed route
        second_route = self.routes.next_healthy_route(task_id, current_route_id=next_route.id)
        self.assertIsNotNone(second_route)
        self.assertNotEqual(second_route.id, next_route.id)

    def test_next_healthy_route_honours_proxy_and_direct_settings(self) -> None:
        self.routes.save({"id": "px-de", "kind": "socks5", "endpoint": "socks5://198.51.100.9:1080", "enabled": True})
        task_id = "task-route-settings"
        self.store.save(DownloadTask(id=task_id, source_url="https://example.com/a.bin", destination=str(self.temp_dir)))
        self.routes.health = lambda pid, force=False: {"healthy": True}

        self.assertEqual(self.routes.next_healthy_route(task_id, current_route_id="direct").id, "px-de")
        # Proxies off and direct excluded as current: nothing else qualifies.
        self.assertIsNone(self.routes.next_healthy_route(task_id, current_route_id="direct", include_proxies=False))
        # From the proxy, direct is the only fallback, and it can be switched off.
        self.assertEqual(self.routes.next_healthy_route(task_id, current_route_id="px-de").id, "direct")
        self.assertIsNone(self.routes.next_healthy_route(task_id, current_route_id="px-de", allow_direct=False))

    def test_proxy_credentials_are_applied_only_at_use_time(self) -> None:
        from engine.routes import with_proxy_credentials
        self.assertEqual(with_proxy_credentials("socks5://1.2.3.4:1080", "jan:p@ss"), "socks5://jan:p%40ss@1.2.3.4:1080")
        self.assertEqual(with_proxy_credentials("http://[2001:db8::1]:8080", "a:b"), "http://a:b@[2001:db8::1]:8080")
        self.assertEqual(with_proxy_credentials("http://1.2.3.4:80", None), "http://1.2.3.4:80")
        # Stored profiles never carry the secret.
        self.routes.save({"id": "px-auth", "kind": "socks5", "endpoint": "socks5://1.2.3.4:1080", "credential_ref": "keychain://proxy/px-auth"})
        stored = next(p for p in self.routes.profiles() if p["id"] == "px-auth")
        self.assertNotIn("@", stored["endpoint"])

    def test_service_failover_on_quota_exception(self) -> None:
        service = EngineService(self.temp_dir / "service_data")
        try:
            # Add task with 2 mirror sources
            url_a = "https://storage.test/quota_mirror/file.zip"
            url_b = "https://mirror.test/backup/file.zip"
            task_dict = service.dispatch("add_task", {
                "url": url_a,
                "destination": str(self.temp_dir / "downloads"),
                "display_name": "file.zip",
            })
            task_id = task_dict["id"]

            # Save alternate URLs on task
            task = service.store.get(task_id)
            task.alternate_urls = [
                {"url": url_b, "provider": "generic", "display_name": "file.zip"}
            ]
            service.store.save(task)

            # Check that initial task source is url_a
            self.assertEqual(service.store.get(task_id).source_url, url_a)

            # Simulate quota lockout and candidate failover through fallback
            service.fallback.lock_quota(task_id, "generic", url_a, error="HTTP 429 Quota Exceeded")
            cands = service.fallback.candidates(task_id, task.alternate_urls)
            self.assertEqual(len(cands), 1)
            self.assertEqual(cands[0]["url"], url_b)

            # Query service RPC for quota locks
            locks = service.dispatch("provider_list_quota_locks", {"task_id": task_id})
            self.assertEqual(len(locks), 1)
            self.assertEqual(locks[0]["outcome"], "quota_exceeded")
        finally:
            service.close()

    def test_route_profile_save_and_delete(self) -> None:
        # 1. Save new route profile
        profile = {"id": "custom-socks", "kind": "socks5", "endpoint": "socks5://127.0.0.1:9050"}
        self.routes.save(profile)
        profiles = self.routes.profiles()
        self.assertTrue(any(p["id"] == "custom-socks" for p in profiles))

        # 2. Delete route profile
        res = self.routes.delete("custom-socks")
        self.assertEqual(res.get("deleted"), "custom-socks")
        profiles_after = self.routes.profiles()
        self.assertFalse(any(p["id"] == "custom-socks" for p in profiles_after))

        # 3. Direct route cannot be deleted
        with self.assertRaises(ValueError):
            self.routes.delete("direct")

    def test_parse_wireguard_conf_validation(self) -> None:
        # Valid WireGuard configuration with realistic 32-byte (44-char) base64 key
        valid_conf = (
            "[Interface]\n"
            "PrivateKey = aGVsbG93b3JsZDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=\n"
            "Address = 10.2.0.2/32\n"
            "DNS = 10.2.0.1\n\n"
            "[Peer]\n"
            "PublicKey = xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg=\n"
            "Endpoint = 198.51.100.5:51820\n"
            "AllowedIPs = 0.0.0.0/0\n"
        )
        parsed = RouteManager.parse_wireguard_conf(valid_conf)
        self.assertEqual(parsed["endpoint"], "198.51.100.5:51820")
        self.assertEqual(parsed["address"], "10.2.0.2/32")

        # Rejects placeholder PrivateKey
        placeholder_conf = valid_conf.replace(
            "aGVsbG93b3JsZDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=",
            "<Your-Proton-PrivateKey>"
        )
        with self.assertRaises(ValueError) as ctx:
            RouteManager.parse_wireguard_conf(placeholder_conf)
        self.assertIn("placeholder", str(ctx.exception).lower())

        # Rejects short/invalid key
        short_key_conf = valid_conf.replace(
            "aGVsbG93b3JsZDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=",
            "shortkey"
        )
        with self.assertRaises(ValueError) as ctx:
            RouteManager.parse_wireguard_conf(short_key_conf)
        self.assertIn("invalid", str(ctx.exception).lower())

    def test_parse_wireguard_conf_provider_export_shapes(self) -> None:
        key = "yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk="
        pub = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
        base = f"[Interface]\nPrivateKey = {key}\nAddress = 10.64.0.1/32\n\n[Peer]\nPublicKey = {pub}\nEndpoint = 1.2.3.4:51820\nAllowedIPs = 0.0.0.0/0\n"

        # Notepad-saved file with a UTF-8 BOM, and CRLF endings.
        self.assertEqual(RouteManager.parse_wireguard_conf("\ufeff" + base)["endpoint"], "1.2.3.4:51820")
        self.assertEqual(RouteManager.parse_wireguard_conf(base.replace("\n", "\r\n"))["endpoint"], "1.2.3.4:51820")

        # Inline comments are stripped, not kept in values.
        commented = base.replace("Endpoint = 1.2.3.4:51820", "Endpoint = 1.2.3.4:51820 # stockholm")
        self.assertEqual(RouteManager.parse_wireguard_conf(commented)["endpoint"], "1.2.3.4:51820")

        # Repeated list keys are merged like wg-quick does.
        repeated = base.replace("AllowedIPs = 0.0.0.0/0", "AllowedIPs = 0.0.0.0/0\nAllowedIPs = ::/0")
        self.assertEqual(RouteManager.parse_wireguard_conf(repeated)["allowed_ips"], "0.0.0.0/0, ::/0")

        # Keys are case-insensitive and canonicalised.
        lowered = base.replace("PrivateKey", "privateKey").replace("AllowedIPs", "allowedips")
        parsed = RouteManager.parse_wireguard_conf(lowered)
        self.assertEqual(parsed["peer"]["AllowedIPs"], "0.0.0.0/0")

        # A real key that happens to contain "xxx" is not a placeholder.
        xxx_key = "aBxxxTF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk="
        RouteManager.parse_wireguard_conf(base.replace(key, xxx_key))

        # Non-base64 keys are rejected even when long enough.
        with self.assertRaises(ValueError):
            RouteManager.parse_wireguard_conf(base.replace(key, "not-a-real-key-but-30-chars!!"))

        # Every peer is kept; the first remains the primary endpoint.
        two = base + f"\n[Peer]\nPublicKey = {key}\nEndpoint = 5.6.7.8:51820\n"
        parsed = RouteManager.parse_wireguard_conf(two)
        self.assertEqual(parsed["endpoint"], "1.2.3.4:51820")
        self.assertEqual([p["Endpoint"] for p in parsed["peers"]], ["1.2.3.4:51820", "5.6.7.8:51820"])

        # Hostname and IPv6 endpoints, preshared keys and PostUp commands with ';' survive.
        airvpn = base.replace("Endpoint = 1.2.3.4:51820", f"Endpoint = europe3.vpn.airdns.org:1637\nPresharedKey = {pub}")
        self.assertEqual(RouteManager.parse_wireguard_conf(airvpn)["endpoint"], "europe3.vpn.airdns.org:1637")
        v6 = base.replace("1.2.3.4:51820", "[2a03:1b20:1:f011::a01f]:51820")
        self.assertEqual(RouteManager.parse_wireguard_conf(v6)["endpoint"], "[2a03:1b20:1:f011::a01f]:51820")
        postup = base.replace("Address = 10.64.0.1/32", "Address = 10.64.0.1/32\nPostUp = a; b")
        self.assertEqual(RouteManager.parse_wireguard_conf(postup)["interface"]["PostUp"], "a; b")

    def test_wireguard_health_check_inactive_without_adapter(self) -> None:
        # Saving a WireGuard route profile
        self.routes.save({
            "id": "wg-nonexistent-tunnel",
            "kind": "wireguard",
            "endpoint": "198.51.100.1:51820",
            "enabled": True,
        })
        # Checking health must NOT return fake True; it must detect adapter absence
        result = self.routes.health("wg-nonexistent-tunnel", force=True)
        self.assertFalse(result["healthy"])
        self.assertIn("not active on Windows", result.get("error", ""))

    def test_proxy_download_probe_structure(self) -> None:
        # Testing download probe against unreachable endpoint returns structured failure
        res = self.routes.test_proxy_download("127.0.0.1:59999", "http_proxy", timeout=0.1)
        self.assertFalse(res["ok"])
        self.assertIn("latency_ms", res)
        self.assertIn("error", res)


if __name__ == "__main__":
    unittest.main()
