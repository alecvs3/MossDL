from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine.custom_downloader import CustomAsyncBackend
from engine.limits import ResourceManager, SchedulerPolicy
from engine.models import ResolvedItem


class _EncryptedRangeHandler(BaseHTTPRequestHandler):
    payload = b""
    range_requests = 0

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.send_header("ETag", '"fixture-v1"')
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        value = self.headers.get("Range", "")
        if value.startswith("bytes="):
            start_text, end_text = value.removeprefix("bytes=").split("-", 1)
            start, end = int(start_text), int(end_text)
            type(self).range_requests += 1
            body = self.payload[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.payload)}")
            self.send_header("ETag", '"fixture-v1"')
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *_args) -> None:
        return


class CustomDownloaderTests(unittest.TestCase):
    def test_mega_ctr_items_use_parallel_range_decryption(self) -> None:
        metadata = {"type": "mega-ctr", "key_a32": [1, 2, 3, 4, 5, 6, 7, 8]}
        plaintext = bytes((index * 31) % 251 for index in range(32 * 1024 * 1024))
        encoder = CustomAsyncBackend(ResourceManager(SchedulerPolicy(bandwidth_bytes_per_second=0)))
        _EncryptedRangeHandler.payload = encoder._mega_ctr_cipher(metadata).encrypt(plaintext)
        _EncryptedRangeHandler.range_requests = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _EncryptedRangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            async def download() -> Path:
                resources = ResourceManager(SchedulerPolicy(
                    max_active_segments=4,
                    max_segments_per_file=4,
                    per_host_transfers=4,
                    bandwidth_bytes_per_second=0,
                ))
                backend = CustomAsyncBackend(resources)
                item = ResolvedItem(
                    provider="transfer.it",
                    source_url="https://transfer.it/t/test",
                    display_name="sample.bin",
                    relative_path="sample.bin",
                    size=len(plaintext),
                    direct_url=f"http://127.0.0.1:{server.server_port}/sample",
                    postprocess=metadata,
                )
                with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                    path = await backend.download(item, directory)
                    result = Path(path).read_bytes()
                    self.assertEqual(result, plaintext)
                    return path

            asyncio.run(download())
            self.assertGreaterEqual(_EncryptedRangeHandler.range_requests, 3)  # probe + two data ranges
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_download_single_auto_populates_size_from_content_length(self) -> None:
        content = b"Content-Length-Wire-Payload-Sample"
        _EncryptedRangeHandler.payload = content
        server = ThreadingHTTPServer(("127.0.0.1", 0), _EncryptedRangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            async def download() -> None:
                resources = ResourceManager(SchedulerPolicy(bandwidth_bytes_per_second=0))
                backend = CustomAsyncBackend(resources)
                item = ResolvedItem(
                    provider="generic",
                    source_url=f"http://127.0.0.1:{server.server_port}/file.bin",
                    display_name="file.bin",
                    size=None,  # unknown at start
                    direct_url=f"http://127.0.0.1:{server.server_port}/file.bin",
                )
                with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                    path = await backend.download(item, directory)
                    self.assertEqual(item.size, len(content))
                    self.assertEqual(Path(path).read_bytes(), content)

            asyncio.run(download())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_download_single_auto_populates_size_from_eof(self) -> None:
        class _ChunkedHandler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()  # no Content-Length header
                self.wfile.write(b"Stream-EOF-Payload-Chunk-1-")
                self.wfile.write(b"Chunk-2")

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _ChunkedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            async def download() -> None:
                resources = ResourceManager(SchedulerPolicy(bandwidth_bytes_per_second=0))
                backend = CustomAsyncBackend(resources)
                item = ResolvedItem(
                    provider="generic",
                    source_url=f"http://127.0.0.1:{server.server_port}/stream",
                    display_name="stream.bin",
                    size=None,
                    direct_url=f"http://127.0.0.1:{server.server_port}/stream",
                )
                with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                    path = await backend.download(item, directory)
                    expected = b"Stream-EOF-Payload-Chunk-1-Chunk-2"
                    self.assertEqual(item.size, len(expected))
                    self.assertEqual(Path(path).read_bytes(), expected)

            asyncio.run(download())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


    def test_host_concurrency_limit_default(self) -> None:
        resources = ResourceManager(SchedulerPolicy(per_host_transfers=8))
        backend = CustomAsyncBackend(resources)
        self.assertEqual(backend._get_host_concurrency_limit("unprofiled.example.com"), 8)
        self.assertEqual(backend._get_host_concurrency_limit("subdomain.other.com"), 8)

    def test_host_concurrency_limit_respects_storage_manager(self) -> None:
        from unittest.mock import MagicMock
        mock_mgr = MagicMock()
        mock_mgr.get_limit.return_value = 2
        resources = ResourceManager(SchedulerPolicy(per_host_transfers=8))
        backend = CustomAsyncBackend(resources, storage_concurrency=mock_mgr)
        self.assertEqual(backend._get_host_concurrency_limit("storage.example.org"), 2)
        mock_mgr.get_limit.assert_called_with("storage.example.org")

    def test_host_concurrency_limit_respects_breaker(self) -> None:
        from unittest.mock import patch
        from engine.concurrency_auditor import concurrency_auditor
        resources = ResourceManager(SchedulerPolicy(per_host_transfers=8))
        backend = CustomAsyncBackend(resources)
        with patch.object(concurrency_auditor, "breaker_admission_limit", return_value=1):
            self.assertEqual(backend._get_host_concurrency_limit("example.com"), 1)


if __name__ == "__main__":
    unittest.main()
