"""One core process serving many transfers, and stopping them without killing it."""
from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine import rust_session
from engine.errors import DownloadCanceled, DownloadPaused
from engine.models import ResolvedItem
from engine.rust_backend import RustTransferBackend, find_transfer_core

FIXTURE = bytes(range(256)) * 512  # 128 KiB
SLOW_FIXTURE_SIZE = 8 * 1024 * 1024


class _Control:
    """The engine's pause/cancel switches, as the adapter expects them."""

    def __init__(self) -> None:
        self.pause = threading.Event()
        self.cancel = threading.Event()


class _Handler(BaseHTTPRequestHandler):
    dropped: set[str] = set()

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path.startswith("/drop-first") and self.path not in _Handler.dropped:
            # Hang up without answering, as an overloaded host does.
            _Handler.dropped.add(self.path)
            self.close_connection = True
            self.connection.shutdown(2)
            return
        if self.path.startswith("/slow"):
            self._serve_slowly()
            return
        start, end = 0, len(FIXTURE) - 1
        requested = self.headers.get("Range")
        if requested and requested.startswith("bytes="):
            raw_start, _, raw_end = requested[6:].partition("-")
            start = int(raw_start or 0)
            end = int(raw_end) if raw_end else len(FIXTURE) - 1
        body = FIXTURE[start:end + 1]
        self.send_response(206 if requested else 200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(FIXTURE)}")
        self.end_headers()
        self.wfile.write(body)

    def _serve_slowly(self):
        # Trickle, so a test has time to ask the transfer to stop mid-flight.
        self.send_response(200)
        self.send_header("Content-Length", str(SLOW_FIXTURE_SIZE))
        self.send_header("Accept-Ranges", "none")
        self.end_headers()
        block = b"\0" * 16384
        sent = 0
        try:
            while sent < SLOW_FIXTURE_SIZE:
                self.wfile.write(block)
                sent += len(block)
                time.sleep(0.02)
        except OSError:
            pass

    def log_message(self, *_args):
        pass


@unittest.skipIf(find_transfer_core() is None, "transfer-core binary is not built")
class RustSessionTests(unittest.TestCase):
    def setUp(self):
        # Threading matters here: several transfers, each segmented, mean
        # dozens of concurrent connections. A single-threaded server would
        # overflow its accept backlog and look like a transport failure.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _item(self, name="fixture.bin", path="/fixture.bin", size=len(FIXTURE)) -> ResolvedItem:
        url = self.base + path
        return ResolvedItem("generic", url, name, size=size, direct_url=url)

    def _pid(self):
        process = rust_session.session()._process
        return process.pid if process is not None else None

    def test_many_transfers_share_one_process(self):
        # The point of the session: fifty files no longer mean fifty launches
        # and fifty cold connection pools.
        backend = RustTransferBackend(min_segment_size=16 * 1024)
        with TemporaryDirectory() as root:
            backend.download(self._item("first.bin"), root)
            first_pid = self._pid()
            for index in range(4):
                backend.download(self._item(f"file-{index}.bin"), root)
            self.assertIsNotNone(first_pid)
            self.assertEqual(self._pid(), first_pid, "a transfer restarted the core")

    def test_concurrent_transfers_all_complete(self):
        backend = RustTransferBackend(min_segment_size=16 * 1024)
        with TemporaryDirectory() as root:
            with ThreadPoolExecutor(max_workers=6) as pool:
                paths = list(pool.map(
                    lambda index: backend.download(self._item(f"parallel-{index}.bin"), root),
                    range(6),
                ))
            self.assertEqual(len(paths), 6)
            for path in paths:
                self.assertEqual(Path(path).read_bytes(), FIXTURE)

    def test_a_dropped_first_connection_is_retried(self):
        # The size probe is the first request of every transfer; one refused or
        # dropped connection there used to fail the file outright.
        backend = RustTransferBackend(min_segment_size=16 * 1024)
        with TemporaryDirectory() as root:
            path = backend.download(self._item("dropped.bin", "/drop-first.bin"), root)
            self.assertEqual(Path(path).read_bytes(), FIXTURE)

    def test_pause_stops_the_transfer_and_leaves_the_process_running(self):
        backend = RustTransferBackend(max_retries=0, min_segment_size=64 * 1024 * 1024)
        control = _Control()
        item = self._item("slow.bin", "/slow.bin", size=SLOW_FIXTURE_SIZE)
        with TemporaryDirectory() as root:
            def pause_shortly():
                time.sleep(1.0)
                control.pause.set()

            threading.Thread(target=pause_shortly, daemon=True).start()
            with self.assertRaises(DownloadPaused):
                backend.download(item, root, control=control)
            # Killing the process was the old way to pause; the core should
            # still be alive and serving.
            self.assertIsNotNone(self._pid())
            backend.download(self._item("after-pause.bin"), root)

    def test_cancel_is_reported_as_a_cancellation(self):
        backend = RustTransferBackend(max_retries=0, min_segment_size=64 * 1024 * 1024)
        control = _Control()
        item = self._item("slow-cancel.bin", "/slow.bin", size=SLOW_FIXTURE_SIZE)
        with TemporaryDirectory() as root:
            def cancel_shortly():
                time.sleep(1.0)
                control.cancel.set()

            threading.Thread(target=cancel_shortly, daemon=True).start()
            with self.assertRaises(DownloadCanceled):
                backend.download(item, root, control=control)

    def test_a_lost_process_is_reported_and_then_replaced(self):
        # A crash must surface as a failure the engine can fall back from,
        # not as a transfer that hangs forever waiting for an answer.
        backend = RustTransferBackend(min_segment_size=16 * 1024)
        with TemporaryDirectory() as root:
            backend.download(self._item("before-kill.bin"), root)
            process = rust_session.session()._process
            self.assertIsNotNone(process)
            process.kill()
            process.wait(timeout=5)
            path = backend.download(self._item("after-kill.bin"), root)
            self.assertEqual(Path(path).read_bytes(), FIXTURE)


if __name__ == "__main__":
    unittest.main()
