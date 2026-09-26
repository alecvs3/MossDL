"""Fault regressions for transfer and timer recovery; no live providers."""
import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from engine.custom_downloader import CustomAsyncBackend
from engine.limits import ResourceManager, SchedulerPolicy
from engine.models import DownloadTask, ResolvedItem
from engine.segment_stealer import DynamicSegmentCoordinator
from engine.service import EngineService, _TaskControl
from engine.timer_scheduler import TimerScheduler
from engine.transport_pool import PooledTransportManager


class HardeningTests(unittest.TestCase):
    def test_ignored_range_probe_does_not_consume_body(self):
        consumed = []

        class Body(httpx.SyncByteStream):
            def __iter__(self):
                consumed.append(True)
                yield b'x' * 8192

        def respond(request):
            return httpx.Response(200, headers={'Content-Length': '8192'},
                                  stream=Body() if request.method == 'GET' else httpx.ByteStream(b''))

        manager = PooledTransportManager()
        manager._clients['direct'] = httpx.Client(transport=httpx.MockTransport(respond))
        try:
            result = manager.probe('https://fixture.invalid/file')
            self.assertFalse(result['ranges'])
            self.assertEqual(consumed, [])
        finally:
            manager.close()

    def test_missing_partial_cannot_publish_manifest_progress(self):
        async def scenario(root):
            size = 4096
            coordinator = DynamicSegmentCoordinator(root / 'file.part.segments.json', size)
            coordinator.load_or_init([(0, size - 1)])
            coordinator.record_progress(0, size)
            coordinator.flush()
            backend = CustomAsyncBackend(ResourceManager(SchedulerPolicy(min_segment_size=1024, max_retries=0)))
            item = ResolvedItem('http', 'https://fixture.invalid/file', 'file', size=size,
                                direct_url='https://fixture.invalid/file')
            metadata = {'size': size, 'ranges': True, 'status_code': 206,
                        'content_range': f'bytes 0-0/{size}', 'validator': '"v1"',
                        'headers': {'etag': '"v1"'}}
            try:
                with patch.object(backend, '_probe', return_value=metadata), \
                     patch.object(backend, '_fetch_range_stealer', side_effect=RuntimeError('must refetch')):
                    with self.assertRaises(Exception):
                        await backend._download_segmented(item, root / 'file', None, None,
                                                          asyncio.get_running_loop())
                self.assertFalse((root / 'file').exists())
            finally:
                backend.transport_pool.close()

        with tempfile.TemporaryDirectory() as raw:
            asyncio.run(scenario(Path(raw)))

    def test_timer_extension_survives_old_waiter(self):
        async def scenario():
            now = [100.0]
            scheduler = TimerScheduler(clock=lambda: now[0])
            scheduler.arm('fixture.invalid', 'part', 2)
            changed = []

            async def advance(seconds):
                now[0] += seconds
                if not changed:
                    scheduler.rearm('fixture.invalid', 'part', seconds=9)
                    changed.append(True)

            with patch('engine.timer_scheduler.asyncio.sleep', side_effect=advance):
                self.assertTrue(await scheduler.wait_for('fixture.invalid', 'part'))
            self.assertGreaterEqual(now[0], 110.0)
        asyncio.run(scenario())

    def test_cancellation_wins_at_expiry(self):
        cancel = threading.Event()
        cancel.set()
        scheduler = TimerScheduler(clock=lambda: 100.0)
        self.assertFalse(asyncio.run(scheduler.wait_until(100.0, cancel_event=cancel)))

    def test_progress_emits_task_updated_with_speed_fields(self):
        """W4/W5 regression: _progress() must emit TaskUpdated carrying speed/bytes so
        the frontend receives live push updates without relying solely on snapshot polling."""
        with TemporaryDirectory() as tmp:
            svc = EngineService(Path(tmp))
            try:
                task = DownloadTask(
                    source_url='https://fixture.invalid/file',
                    destination=tmp,
                    display_name='file',
                    size=1024 * 1024,
                )
                svc.store.save(task)

                # Simulate two progress ticks separated by more than 250 ms so the
                # throttle gate opens and save_progress + TaskUpdated are both called.
                with patch('engine.service.time') as mock_time:
                    t0 = time.time()
                    mock_time.time.side_effect = [t0, t0 + 0.3, t0 + 0.3, t0 + 0.3,
                                                  t0 + 0.3, t0 + 0.3, t0 + 0.3]
                    mock_time.monotonic.return_value = t0 + 0.3
                    svc._telemetry_samples[task.id] = (t0, 0, 0.0)
                    svc._last_progress_save[task.id] = 0.0  # force save on first call
                    svc._progress(task, 512 * 1024)

                # Find the TaskUpdated event for this task
                events = svc.store.events_since(0, limit=50)
                progress_events = [e for e in events
                                   if e.get('event_type') == 'TaskUpdated'
                                   and e.get('task_id') == task.id
                                   and 'speed_bytes_per_second' in (e.get('payload') or {})]
                self.assertTrue(progress_events,
                                "No TaskUpdated event with speed_bytes_per_second was emitted by _progress()")
                payload = progress_events[-1]['payload']
                self.assertIn('completed_bytes', payload)
                self.assertIn('size', payload)
                self.assertIn('average_speed_bytes_per_second', payload)
                self.assertIn('eta_seconds', payload)
            finally:
                svc.close()

    def test_stall_watchdog_drop_does_not_trigger_backend_fallback(self):
        """When a task is dropped by STORAGE_HOST_STALL_DROPPED, it must NOT fall back from rust to custom."""
        with TemporaryDirectory() as tmp:
            svc = EngineService(Path(tmp))
            try:
                task = DownloadTask(
                    source_url='https://node42.datanodes.to/file.part4.rar',
                    destination=tmp,
                    display_name='file.part4.rar',
                    backend='rust',
                    state='queued',
                    resolved=[
                        ResolvedItem(
                            provider='datanodes',
                            source_url='https://node42.datanodes.to/file.part4.rar',
                            display_name='file.part4.rar',
                            direct_url='https://node42.datanodes.to/stream/file.part4.rar',
                            size=1024 * 1024,
                        )
                    ]
                )
                svc.store.save(task)
                control = _TaskControl()
                svc._controls[task.id] = control

                def simulate_stall_drop(*args, **kwargs):
                    dropped_task = svc.store.get(task.id)
                    dropped_task.state = 'queued'
                    dropped_task.error = 'node42.datanodes.to starved this stream (<1MB >45s); queued for retry'
                    svc.store.save(dropped_task)
                    control.cancel.set()
                    from engine.errors import DownloadCanceled
                    raise DownloadCanceled("Dropped starved secondary stream")

                with patch.object(svc.resolution_broker, 'resolve', return_value=task.resolved), \
                     patch.object(svc, '_download_item_with_refresh', side_effect=simulate_stall_drop), \
                     patch.object(svc.rust_backend, 'available', return_value=True):
                    asyncio.run(svc._run_task_async(task.id, {}, control, 'rust'))

                current = svc.store.get(task.id)
                self.assertEqual(current.backend, 'rust')
                self.assertEqual(current.state, 'queued')
                self.assertIn('starved this stream', current.error)
            finally:
                svc.close()

    def test_multipart_backend_affinity_preserved_with_rust(self):
        """Multipart packages must maintain backend consistency with rust even if task.backend was set to custom."""
        with TemporaryDirectory() as tmp:
            svc = EngineService(Path(tmp))
            try:
                task = DownloadTask(
                    source_url='https://node42.datanodes.to/game.part3.rar',
                    destination=tmp,
                    display_name='game.part3.rar',
                    backend='custom',
                    package_key='pkg_game_123',
                    package_part_number=3,
                    package_part_count=4,
                    state='queued',
                    resolved=[
                        ResolvedItem(
                            provider='datanodes',
                            source_url='https://node42.datanodes.to/game.part3.rar',
                            display_name='game.part3.rar',
                            direct_url='https://node42.datanodes.to/stream/game.part3.rar',
                            size=1024 * 1024,
                        )
                    ]
                )
                svc.store.save(task)
                control = _TaskControl()

                with patch.object(svc.resolution_broker, 'resolve', return_value=task.resolved), \
                     patch.object(svc.rust_backend, 'available', return_value=True), \
                     patch.object(svc.rust_backend, 'download', return_value=Path(tmp) / 'game.part3.rar'), \
                     patch.object(svc, '_download_item_with_refresh', return_value=Path(tmp) / 'game.part3.rar'), \
                     patch.object(svc, '_on_download_completed', return_value=None):
                    (Path(tmp) / 'game.part3.rar').write_bytes(b'x' * 1024 * 1024)
                    asyncio.run(svc._run_task_async(task.id, {}, control, None))

                current = svc.store.get(task.id)
                self.assertEqual(current.backend, 'rust', "Multipart task should have affinity to rust backend")
            finally:
                svc.close()

