from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import sqlite3
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from tempfile import TemporaryDirectory

from engine.service import EngineService, pre_dispatch
from engine.db import TaskStore
from engine.segment_stealer import DynamicSegmentCoordinator


class _ChaosFailingHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()


    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.end_headers()
        try:
            self.wfile.write(bQX* 50)
            self.wfile.flush()
        except Exception:
            pass


    def log_message(self, *_args):
        return


class ChaosResilienceTests(unittest.TestCase):
    """Acceptance suite testing fault tolerance and recovery under failure conditions."""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)


    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass


    def test_rpc_malformed_frames_and_scope_validation(self):
        """Verify that malformed JSON-RPC frames and missing scopes are rejected cleanly."""
        service = EngineService(self.root / "rpc-chaos")
        try:
            # Missing jsonrpc envelope
            with self.assertRaisesRegex(ValueError, "jsonrpc 2.0 envelope is required"):
                pre_dispatch(service, {"jsonrpc": "1.0", "method": "api_info"}, {"client_id": "c1", "scopes": ["read"]})


            # Missing method
            with self.assertRaisesRegex(ValueError, "method is required"):
                pre_dispatch(service, {"jsonrpc": "2.0", "method": ""}, {"client_id": "c1", "scopes": ["read"]})


            # Unknown method
            with self.assertRaisesRegex(ValueError, "unknown method"):
                pre_dispatch(service, {"jsonrpc": "2.0", "method": "nonexistent_method"}, {"client_id": "c1", "scopes": ["read"]})


            # Unauthorized (missing auth)
            with self.assertRaisesRegex(PermissionError, "unauthorized: missing authentication context"):
                pre_dispatch(service, {"jsonrpc": "2.0", "method": "api_info"}, None)


            # Insufficient scopes
            with self.assertRaisesRegex(PermissionError, "insufficient scope"):
                pre_dispatch(
                    service,
                    {"jsonrpc": "2.0", "method": "add_task", "params": {"url": "https://example.com/file.bin", "idempotency_key": "k1"}},
                    {"client_id": "c1", "scopes": ["read"]}
                )


            # Client identity mismatch
            with self.assertRaisesRegex(PermissionError, "client identity does_not_match".replace("_", " ")):
                pre_dispatch(
                    service,
                    {"jsonrpc": "2.0", "method": "api_info", "client_id": "imposter"},
                    {"client_id": "c1", "scopes": ["read"]}
                )
        finally:
            service.close()


    def test_corrupted_sqlite_task_store_recovery(self):
        """Verify that corrupted SQLite database files raise database errors cleanly."""
        db_dir = self.root / "db_test"
        db_dir.mkdir(exist_ok=True)
        db_path = db_dir / "corrupt.sqlite3"
        db_path.write_bytes(b"MALFORMED_SQLITE_HEADER_GIBBERISH_1234567890")
        store = None
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                store = TaskStore(db_path)
        finally:
            if store and hasattr(store, "db") and store.db:
                store.db.close()


    def test_segment_coordinator_interrupted_progress_recovery(self):
        """Verify DynamicSegmentCoordinator resilience with zero or stalled workers."""
        manifest = self.root / "segments.json"
        coord = DynamicSegmentCoordinator(
            manifest_path=manifest,
            total_size=32 * 1024 * 1024,
            min_steal_bytes=1024 * 1024,
        )
        initial = [
            (0, 8 * 1024 * 1024 - 1),
            (8 * 1024 * 1024, 16 * 1024 * 1024 - 1),
            (16 * 1024 * 1024, 24 * 1024 * 1024 - 1),
            (24 * 1024 * 1024, 32 * 1024 * 1024 - 1),
        ]
        coord.load_or_init(initial)

        w1 = coord.claim_work("w1")
        w2 = coord.claim_work("w2")
        w3 = coord.claim_work("w3")
        w4 = coord.claim_work("w4")
        self.assertIsNotNone(w1)
        self.assertIsNotNone(w2)

        stolen = coord.claim_work("w1")
        self.assertIsNotNone(stolen)
        self.assertEqual(stolen.worker_id, "w1")
        self.assertGreater(stolen.remaining, 0)


    def test_unresponsive_network_abort_resilience(self):
        """Verify engine handles unexpected network connection dropouts without crashing."""
        server = HTTPServer(("127.0.0.1", 0), _ChaosFailingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            service = EngineService(self.root / "network-chaos")
            try:
                task = service.dispatch("add_task", {
                    "url": f"http://127.0.0.1:{server.server_port}/chaos-file",
                    "destination": str(self.root / "downloads"),
                })
                self.assertIsNotNone(task["id"])
                stored = service.store.get(task["id"])
                self.assertIsNotNone(stored)
            finally:
                service.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
