from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import shutil
import unittest
from pathlib import Path

from engine.capture import rank_candidates
from engine.collection import CollectionPlanner
from engine.db import TaskStore
from engine.media_pipeline import parse_hls, parse_media
from engine.models import ResolutionContext, ResolvedItem
from engine.provider_health import ProviderHealthMonitor
from engine.provider_sdk import sign_catalog, verify_catalog
from engine.protocols import resolve_protocol
from engine.resolution import ResolutionBroker
from engine.universal_resolver import UniversalResolver


class RoadmapFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / ".test-artifacts" / "roadmap" / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_capture_ranking_redacts_sensitive_headers(self) -> None:
        ranked = rank_candidates([
            {"url": "https://site.test/page", "headers": {"Authorization": "secret"}},
            {"url": "https://cdn.test/video.mp4", "mime": "video/mp4", "headers": {"Cookie": "x=y"}},
        ])
        self.assertEqual(ranked[0].url, "https://cdn.test/video.mp4")
        self.assertNotIn("Cookie", ranked[0].headers)

    def test_hls_plan_has_init_ranges_and_variant_selection(self) -> None:
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=500\nhigh.m3u8\n"
        plan = parse_hls("https://media.test/master.m3u8", master)
        self.assertEqual(plan.selected_variant["bandwidth"], 500)
        media = "#EXTM3U\n#EXT-X-MAP:URI=init.mp4,BYTERANGE=10@0\n#EXTINF:2,\n#EXT-X-BYTERANGE:20@10\nseg.mp4\n"
        plan = parse_hls("https://media.test/media.m3u8", media)
        self.assertTrue(plan.segments[0].initialization)
        self.assertEqual(plan.segments[1].byte_range, "20@10")

    def test_resolution_cache_persists_no_direct_url(self) -> None:
        store = TaskStore(self.root / "test.sqlite3")
        try:
            broker = ResolutionBroker(store)
            context = ResolutionContext("https://site.test/file", provider_id="p")
            broker.resolve(context, lambda _: [ResolvedItem("p", context.source_url, "x.bin", direct_url="https://cdn.test/x?sig=secret")])
            cached = store.get_resolution_cache(context.source_url)
            self.assertNotIn("sig=secret", json.dumps(cached))
            self.assertIsNone(cached["metadata"]["items"][0].get("direct_url"))
        finally:
            store.close()

    def test_collection_selection_and_health_snapshot_are_durable(self) -> None:
        store = TaskStore(self.root / "test.sqlite3")
        try:
            plan = CollectionPlanner().create("c1", "https://site.test/a", "p", [{"id": "x", "name": "x.bin"}])
            store.save_collection_plan(plan)
            plan.items[0].selected = True
            store.save_collection_plan(plan)
            self.assertTrue(store.get_collection_plan("c1")["items"][0]["selected"])
            monitor = ProviderHealthMonitor(store)
            monitor.record("p", "success")
            monitor.record("p", "failed", "expired_url")
            self.assertEqual(store.list_provider_health_snapshots()[0]["total_attempts"], 2)
        finally:
            store.close()

    def test_universal_resolver_protocols_and_signed_catalog(self) -> None:
        html = '<meta property="og:video" content="/movie.mp4"><a href="/doc.pdf">doc</a>'
        result = UniversalResolver(fetcher=lambda _: ("text/html", html)).inspect("https://site.test/page")
        self.assertEqual(result["candidates"][0]["url"], "https://site.test/movie.mp4")
        self.assertEqual(resolve_protocol("sftp://host/path/file.bin")[0].metadata["protocol"], "sftp")
        signed = sign_catalog({"providers": [{"id": "p"}]}, "test-secret")
        self.assertEqual(verify_catalog(signed, "test-secret")["providers"][0]["id"], "p")

    def test_turnstile_needs_user_routes_to_captcha_cascade(self) -> None:
        from engine.service import EngineService
        from engine.errors import NeedsUser
        from engine.models import ResolvedItem
        from unittest.mock import patch, AsyncMock

        service = EngineService(str(self.root / "tm_turnstile"))
        try:
            task = service.dispatch("add_task", {"url": "https://vikingfile.com/f/A94TpVDc66"})
            task_id = task["id"]

            # Simulate provider raising NeedsUser on initial resolve, then succeeding when token is returned
            def mock_resolve(url, secrets=None, *args, **kwargs):
                if secrets and (secrets.get("turnstile_token") or secrets.get("captcha_solution")):
                    return [ResolvedItem("vikingfile", url, "sample.rar",
                                         direct_url="https://vikingfile.com/d/sample.rar", size=1024)]
                raise NeedsUser("Cloudflare Turnstile verification required for Vikingfile", "turnstile",
                                {"sitekey": "0x4AAAAAAAgbsMNBuk2d3Qp6", "url": "https://vikingfile.com/f/A94TpVDc66",
                                 "file_id": "A94TpVDc66", "filename": "sample.rar"})

            with patch.object(service.plugins, "resolve_chain", side_effect=mock_resolve):
                # When UI solve_challenge is called, verify token is returned
                service.captcha.interactive_ui.solve = AsyncMock(return_value={"token": "mock_cf_token_123"})
                # Run download task once
                service.dispatch("download_task", {"id": task_id})
                import time
                for _ in range(30):
                    time.sleep(0.1)
                    if service.store.get(task_id).state != "resolving":
                        break

                updated = service.store.get(task_id)
                # The loop above breaks the moment the task leaves `resolving`, so it
                # can legitimately be sampled in `preflight` -- a healthy transient
                # state on the way to `downloading`. Omitting it made this assertion
                # racy rather than strict.
                self.assertIn(updated.state,
                              {"queued", "preflight", "downloading", "completed", "needs_user"})
                # Active UI challenge or solved challenge was properly registered
                challenges = service.store.list_captcha_challenges(task_id)
                self.assertTrue(len(challenges) >= 1)
                self.assertEqual(challenges[0]["captcha_type"], "turnstile")
                self.assertEqual(challenges[0]["params"].get("site_key"), "0x4AAAAAAAgbsMNBuk2d3Qp6")
        finally:
            service.close()

    def test_multi_item_download_concurrency(self) -> None:
        from engine.service import EngineService
        from engine.models import ResolvedItem
        from unittest.mock import patch
        import asyncio

        service = EngineService(str(self.root / "tm_multi_item"))
        try:
            task = service.dispatch("add_task", {"url": "https://drive.google.com/drive/folders/testfolder"})
            task_id = task["id"]

            items = [
                ResolvedItem("google-drive", f"https://drive.google.com/file{i}", f"file_{i}.txt",
                             direct_url=f"https://drive.usercontent.google.com/download?id={i}", size=1024)
                for i in range(4)
            ]

            active_downloads = []
            max_concurrent = 0

            async def mock_download_with_refresh(task_obj, item_index, item_obj, dest, progress, control, route, backend, secrets, metrics=None):
                nonlocal max_concurrent
                active_downloads.append(item_index)
                if len(active_downloads) > max_concurrent:
                    max_concurrent = len(active_downloads)
                progress(1024)
                await asyncio.sleep(0.05)
                active_downloads.remove(item_index)
                target = Path(dest) / item_obj.display_name
                # The shared service contract verifies the provider-reported
                # size before finalization, so this fixture must represent a
                # complete 1 KiB transfer.
                target.write_bytes(b"x" * 1024)
                return target

            with patch.object(service.plugins, "resolve_chain", return_value=items):
                with patch.object(service, "_download_item_with_refresh", side_effect=mock_download_with_refresh):
                    service.dispatch("download_task", {"id": task_id})
                    import time
                    for _ in range(50):
                        time.sleep(0.1)
                        if service.store.get(task_id).state == "completed":
                            break
                    completed_task = service.store.get(task_id)
                    self.assertEqual(completed_task.state, "completed")
                    self.assertEqual(completed_task.completed_bytes, 4 * 1024)
                    # Proves that items were downloaded concurrently (> 1 in flight)
                    self.assertGreaterEqual(max_concurrent, 2)
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
