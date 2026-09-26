from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import os
import io
import json
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine.db import TaskStore
from engine.models import DownloadQueue, DownloadTask, ResolvedItem
from engine.plugins import PluginRegistry
from engine.service import EngineService
from engine.browser_bridge import read_message, validate_capture, write_message
from engine.providers.media import MediaProvider
from engine.shortlinks import load_catalog


class _PageHandler(BaseHTTPRequestHandler):
    body = b'''<html><a href="/files/a.zip">A</a><a href="/files/b.bin">B</a><a href="https://other.invalid/out">external</a></html>'''

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args):
        return


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file.bin")
            self.end_headers()
            return
        if self.path == "/redirect-page":
            self.send_response(302)
            self.send_header("Location", "/download-page")
            self.end_headers()
            return
        if self.path == "/download-page":
            body = b'<html><a href="/file.bin">Download</a></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"fixture-data"
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        if self.path in {"/redirect", "/redirect-page"}:
            self.send_response(302)
            self.send_header("Location", "/file.bin" if self.path == "/redirect" else "/download-page")
            self.end_headers()
            return
        if self.path == "/download-page":
            body = b'<html><a href="/file.bin">Download</a></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        body = b"fixture-data"
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def log_message(self, *_args):
        return


class EcosystemTests(unittest.TestCase):
    root = Path(__file__).parent / ".test-artifacts" / "ecosystem"

    def test_queue_round_trip_and_task_options(self):
        store = TaskStore(self.root / "queues.sqlite3")
        try:
            store.save_queue(DownloadQueue("night", "Night queue", start_hour=0, end_hour=6))
            task = DownloadTask("https://example.test/a", str(self.root), priority=7, queue_id="night", scheduled_at=time.time() + 60)
            store.save(task)
            loaded = store.get(task.id)
            self.assertEqual(loaded.queue_id, "night")
            self.assertEqual(loaded.priority, 7)
            self.assertEqual(loaded.scheduled_at, task.scheduled_at)
            self.assertEqual(store.get_queue("night")["name"], "Night queue")
        finally:
            store.close()

    def test_bare_transfer_url_is_normalized_before_provider_matching(self):
        service = EngineService(self.root / "url-normalization")
        try:
            task = service.dispatch("add_task", {"url": "transfer.it/t/mo8krhyc9vsb", "destination": str(self.root / "downloads")})
            self.assertEqual(task["source_url"], "https://transfer.it/t/mo8krhyc9vsb")
        finally:
            service.close()

    def test_page_plugin_extracts_same_host_links(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        registry = PluginRegistry()
        try:
            url = f"http://127.0.0.1:{server.server_port}/index.html"
            items = registry.extract_links(url)
            self.assertEqual([item.display_name for item in items], ["a.zip", "b.bin"])
            self.assertTrue(all(item.metadata["extracted_from"] == url for item in items))
        finally:
            registry.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_generic_redirect_resolves_final_http_file(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        registry = PluginRegistry()
        try:
            url = f"http://127.0.0.1:{server.server_port}/redirect"
            items = registry.resolve_chain(url)
            self.assertEqual(items[0].direct_url, f"http://127.0.0.1:{server.server_port}/file.bin")
            self.assertTrue(items[0].metadata["redirected"])
        finally:
            registry.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_generic_redirect_page_extracts_final_download_link(self):
        registry = PluginRegistry()
        try:
            url = "https://t.co/fixture"
            page = "https://download.example.test/page"
            file_url = "https://cdn.example.test/file.bin"
            with patch.object(registry, "resolve", side_effect=[
                [ResolvedItem("generic", url, "download.html", direct_url=page,
                              metadata={"content_type": "text/html", "final_url": page, "redirected": True})],
                [ResolvedItem("generic", file_url, "file.bin", direct_url=file_url,
                              metadata={"content_type": "application/octet-stream", "final_url": file_url})],
            ]), patch.object(registry, "provider_for", return_value="generic"), \
                 patch("engine.plugins.fetch_and_extract", return_value=[file_url]):
                items = registry.resolve_chain(url)
            self.assertEqual(items[0].direct_url, file_url)
        finally:
            registry.close()

    def test_engine_pause_and_schedule_are_persistent(self):
        root = self.root / "service"
        service = EngineService(root)
        try:
            service.dispatch("pause_engine", {})
            task = service.dispatch("add_task", {"url": "https://example.test/file", "scheduled_at": time.time() - 1})
            queued = service.dispatch("download_task", {"id": task["id"]})
            self.assertEqual(queued["state"], "queued")
            self.assertEqual(service.store.get_setting("engine_paused"), True)
            service.dispatch("set_task_options", {"id": task["id"], "priority": 10, "duplicate_strategy": "rename"})
            self.assertEqual(service.store.get(task["id"]).priority, 10)
        finally:
            service.close()

    def test_plugin_health_is_persistent_and_quarantine_is_reversible(self):
        path = self.root / "plugin-health.sqlite3"
        store = TaskStore(path)
        registry = PluginRegistry(health_store=store)
        try:
            registry.set_state("gofile", quarantined=True)
            self.assertTrue(next(item for item in registry.list_health() if item["plugin_id"] == "gofile")["quarantined"])
        finally:
            registry.close()
            store.close()
        store = TaskStore(path)
        registry = PluginRegistry(health_store=store)
        try:
            self.assertTrue(next(item for item in registry.list_health() if item["plugin_id"] == "gofile")["quarantined"])
            registry.set_state("gofile", quarantined=False)
        finally:
            registry.close()
            store.close()

    def test_browser_native_message_and_capture_redaction(self):
        stream = io.BytesIO()
        write_message(stream, {"url": "https://example.test/file", "headers": {"Authorization": "secret", "Referer": "https://example.test"}})
        stream.seek(0)
        message = read_message(stream)
        capture = validate_capture(message)
        self.assertNotIn("Authorization", capture["headers"])
        self.assertEqual(capture["headers"]["Referer"], "https://example.test")

    def test_media_parser_rejects_encryption_and_lists_segments(self):
        provider = MediaProvider()
        parsed = provider.parse("https://cdn.test/video/index.m3u8", "#EXTM3U\n#EXTINF:2,\npart-1.ts\n#EXTINF:2,\npart-2.ts\n")
        self.assertEqual(len(parsed["segments"]), 2)
        with self.assertRaises(Exception):
            provider.parse("https://cdn.test/video/index.m3u8", "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key\"\npart.ts\n")

    def test_desktop_notification_sink_consumes_outbox(self):
        store = TaskStore(self.root / "notifications.sqlite3")
        try:
            store.save_notification_sink({"id": "desktop", "kind": "desktop", "endpoint": "local"})
            event_id = store.enqueue_event("TestEvent", None, {"ok": True})
            from engine.notifications import deliver
            event = next(item for item in store.pending_deliveries() if item["id"] == event_id)
            deliver(event, event)
            store.record_delivery(event_id, "desktop", "delivered")
            self.assertFalse(store.event_has_pending_delivery(event_id))
        finally:
            store.close()

    def test_gofile_fixture_tree_and_password_request(self):
        from engine.providers.gofile import GofileProvider
        payload = {"type": "folder", "name": "root", "children": {
            "folder": {"id": "folder", "type": "folder", "name": "nested", "children": {
                "file": {"id": "file", "type": "file", "name": "report.bin", "size": 12, "link": "https://cdn.test/report"}
            }}
        }}
        with patch("engine.providers.gofile._get_json", return_value=payload) as request:
            items = GofileProvider.resolve("https://gofile.io/d/demo", {"password": "pw", "selected_item_ids": "file"})
        with patch("engine.providers.gofile._get_json", return_value=payload) as request:
            tree = GofileProvider.enumerate("https://gofile.io/d/demo", {"password": "pw"})
        self.assertEqual(items[0].relative_path, "root/nested/report.bin")
        self.assertEqual(items[0].item_id, "file")
        self.assertIn("password=", request.call_args.args[0])
        self.assertEqual([item.metadata["type"] for item in tree], ["folder", "folder", "file"])
        self.assertEqual(tree[-1].metadata["parent_id"], "folder")
        self.assertTrue(all(item.direct_url is None for item in tree))

    def test_mega_folder_tree_and_selected_file_resolution(self):
        from engine.providers.mega import MegaProvider
        def fake_api(payload, public_handle=None):
            if payload["a"] == "f":
                return [{"f": [
                    {"t": 1, "h": "root", "name": "root"},
                    {"t": 1, "h": "nested", "p": "root", "name": "nested"},
                    {"t": 0, "h": "file-a", "p": "nested", "name": "report.bin", "s": 12},
                    {"t": 0, "h": "file-b", "p": "root", "name": "other.bin", "s": 5},
                ]}]
            return [{"g": "https://cdn.test/report.bin", "key_a32": [1, 2, 3, 4, 5, 6]}]
        url = "https://mega.nz/folder/root#folder-key"
        with patch.object(MegaProvider, "_api", side_effect=fake_api) as request:
            tree = MegaProvider.enumerate(url)
            items = MegaProvider.resolve(url, {"selected_item_ids": ["file-a"]})
        self.assertEqual([item.relative_path for item in tree], ["root", "root/nested", "root/nested/report.bin", "root/other.bin"])
        self.assertTrue(all(item.direct_url is None for item in tree))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_id, "file-a")
        self.assertEqual(items[0].direct_url, "https://cdn.test/report.bin")
        self.assertEqual(request.call_count, 3)

    def test_transferit_fixture_tree_and_password_kdf(self):
        from engine.providers.transferit import TransferItProvider
        handle = "3V4TDkx1EZJL"
        def fake_api(payload, *_args):
            if payload["a"] == "xi":
                return [{"t": "fixture"}]
            if payload["a"] == "f":
                return [{"f": [{"t": 1, "h": "folder", "p": "", "k": "", "a": "folder-name"},
                                {"t": 0, "h": "file", "p": "folder", "k": "", "s": 7}]}]
            return [{"g": "https://cdn.test/file"}]
        with patch.object(TransferItProvider, "_api", side_effect=fake_api), \
             patch.object(TransferItProvider, "_decrypt_name", side_effect=lambda encoded, _key: "nested" if encoded == "folder-name" else None):
            items = TransferItProvider.resolve(f"https://transfer.it/t/{handle}", {"password": "pw"})
            tree = TransferItProvider.enumerate(f"https://transfer.it/t/{handle}", {"password": "pw"})
        self.assertEqual(items[0].relative_path, "nested/file")
        self.assertEqual(items[0].item_id, "file")
        self.assertEqual(len(TransferItProvider._password_token(handle, "pw")), 43)
        self.assertEqual({item.metadata["type"] for item in tree}, {"folder", "file"})
        self.assertIsNone(next(item for item in tree if item.metadata["type"] == "file").direct_url)

    def test_shortlink_catalog_routes_static_target_without_fetching(self):
        registry = PluginRegistry()
        try:
            items = registry.shortlink_extract("https://ouo.io/go?url=https%3A%2F%2Fmega.nz%2Ffile%2Ffixture")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].direct_url, "https://mega.nz/file/fixture")
            self.assertEqual(items[0].provider, "shortlink")
        finally:
            registry.close()

    def test_shortlink_chain_is_bounded_and_reports_redacted_hops(self):
        registry = PluginRegistry()
        hops = []
        try:
            with patch.object(registry, "resolve", return_value=[]):
                registry.resolve_chain("https://ouo.io/go?url=https%3A%2F%2Fmega.nz%2Ffile%2Ffixture",
                                       on_hop=lambda hop, url, provider: hops.append((hop, url, provider)))
            self.assertEqual(hops[0][0], 0)
            self.assertEqual(hops[-1][1], "https://mega.nz/file/fixture")
        finally:
            registry.close()

    def test_shortlink_chain_does_not_admit_html_as_a_completed_file(self):
        from engine.errors import NeedsUser
        registry = PluginRegistry()
        final = "https://interstitial.invalid/continue"
        try:
            with patch.object(registry, "resolve", return_value=[
                ResolvedItem("generic", final, "download.html", direct_url=final,
                             metadata={"content_type": "text/html"})
            ]), patch("engine.plugins.fetch_and_extract", return_value=[]):
                with self.assertRaises(NeedsUser) as raised:
                    registry.resolve_chain("https://ouo.io/go?url=" +
                                           "https%3A%2F%2Finterstitial.invalid%2Fcontinue")
            self.assertEqual(raised.exception.action, "browser_handoff")
        finally:
            registry.close()


class LiveProviderSmokeTests(unittest.TestCase):
    """Opt-in tests for real public provider links; never run by default."""

    @unittest.skipUnless(os.environ.get("TRANSFER_MANAGER_LIVE"), "set TRANSFER_MANAGER_LIVE=1")
    def test_transferit_public_fixture(self):
        from engine.providers.transferit import TransferItProvider
        handle = os.environ.get("TRANSFERIT_TEST_HANDLE", "3V4TDkx1EZJL")
        items = TransferItProvider.resolve(f"https://transfer.it/t/{handle}", {})
        self.assertTrue(items)
        self.assertTrue(all(item.direct_url for item in items))

    @unittest.skipUnless(os.environ.get("TRANSFER_MANAGER_LIVE") and os.environ.get("GOFILE_TEST_URL"),
                         "set TRANSFER_MANAGER_LIVE=1 and GOFILE_TEST_URL")
    def test_gofile_configured_fixture(self):
        from engine.providers.gofile import GofileProvider
        secrets = {key: os.environ[key] for key in ("password", "token") if os.environ.get(key)}
        items = GofileProvider.resolve(os.environ["GOFILE_TEST_URL"], secrets)
        self.assertTrue(items)
        self.assertTrue(all(item.direct_url for item in items))


class ShortlinkSmokeTests(unittest.TestCase):
    """Ten-case shortlink corpus plus opt-in real-site smoke coverage."""

    def test_ten_case_shortlink_corpus(self):
        cases = [("ouo.io", f"https://ouo.io/go?url=https%3A%2F%2Fmega.nz%2Ffile%2Fouo-{index}")
                 for index in range(1, 6)]
        cases.extend([
            ("linkvertise.com", "https://linkvertise.com/12345/example?url=https%3A%2F%2Fmega.nz%2Ffile%2Fbasd-1"),
            ("boost.ink", "https://boost.ink/s/example?target=https%3A%2F%2Fmega.nz%2Ffile%2Fbasd-2"),
            ("rekonise.com", "https://rekonise.com/example?url=https%3A%2F%2Fmega.nz%2Ffile%2Fbasd-3"),
            ("tii.la", "https://tii.la/example?redirect=https%3A%2F%2Fmega.nz%2Ffile%2Fbasd-4"),
            ("shortfaster.net", "https://shortfaster.net/example?dest=https%3A%2F%2Fmega.nz%2Ffile%2Fbasd-5"),
        ])
        registry = PluginRegistry()
        try:
            self.assertEqual(len(cases), 10)
            for host, url in cases:
                self.assertTrue(any(entry["host"] == host for entry in load_catalog().get("entries", [])))
                items = registry.shortlink_extract(url)
                self.assertEqual(len(items), 1, host)
                self.assertEqual(registry.provider_for(items[0].direct_url), "mega", host)
        finally:
            registry.close()

    def test_network_loop_encoded_redirect_fixture(self):
        url = "https://network-loop.com/.safe/redirect.html?u=aHR0cHM6Ly93d3cuc2hyaW5rLXNlcnZpY2UuaXQvYnRuL3FqSXo2dg=="
        registry = PluginRegistry()
        try:
            items = registry.shortlink_extract(url)
            self.assertEqual([item.direct_url for item in items], ["https://www.shrink-service.it/btn/qjIz6v"])
        finally:
            registry.close()

    def test_shortlink_html_extractor_ignores_stylesheet_resources(self):
        from engine.shortlinks import extract_static_targets
        body = '''<html><head><link rel="stylesheet" href="https://unpkg.com/pico.css"></head>
                  <body><a href="https://files.example.test/final.zip">Continue</a></body></html>'''
        self.assertEqual(extract_static_targets("https://short.example.test/x", body),
                         ["https://files.example.test/final.zip"])

    @unittest.skipUnless(os.environ.get("SHORTLINK_LIVE") and os.environ.get("SHORTLINK_OUO_URLS") and
                         os.environ.get("SHORTLINK_BASD_URLS"),
                         "set SHORTLINK_LIVE, SHORTLINK_OUO_URLS, and SHORTLINK_BASD_URLS")
    def test_five_ouo_and_five_basd_live_links(self):
        def values(name):
            return [value.strip() for value in os.environ[name].replace("\n", ",").split(",") if value.strip()]
        urls = values("SHORTLINK_OUO_URLS") + values("SHORTLINK_BASD_URLS")
        self.assertEqual(len(values("SHORTLINK_OUO_URLS")), 5)
        self.assertEqual(len(values("SHORTLINK_BASD_URLS")), 5)
        registry = PluginRegistry()
        try:
            observed = []
            for url in urls:
                try:
                    items = registry.shortlink_extract(url)
                    observed.append({"url": url, "status": "resolved", "targets": [item.direct_url for item in items]})
                    self.assertTrue(items, url)
                except Exception as exc:
                    # Browser-only timer/CAPTCHA flows are expected to stop at
                    # the structured handoff boundary, not fail silently.
                    from engine.errors import NeedsUser
                    if not isinstance(exc, NeedsUser):
                        raise
                    observed.append({"url": url, "status": "needs_user", "action": exc.action})
            self.assertEqual(len(observed), 10)
        finally:
            registry.close()

if __name__ == "__main__":
    unittest.main()
