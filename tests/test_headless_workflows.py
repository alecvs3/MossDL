from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

from engine.browser_bridge import PROTOCOL_VERSION as BROWSER_PROTOCOL_VERSION, NativeProtocolSession
from engine.cli import main as cli_main
from engine.db import TaskStore
from engine.http_api import create_http_server, required_scope
from engine.models import ArchiveJob, CollectionItem, CollectionPlan, DownloadTask
from engine.platform import API_VERSION, JsonRpcEnvelope, JsonRpcResponse
from engine.service import (
    EngineService,
    _HEADLESS_MUTATIONS,
    _HEADLESS_OPERATION_SCOPES,
    authenticate_headless_token,
    headless_required_scope,
    pre_dispatch,
    serve,
)


class HeadlessWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = EngineService(self.root)
        self.client_res = self.service.dispatch(
            "client_register",
            {"label": "test-admin", "scopes": ["read", "enqueue", "account_admin", "worker_control"]},
        )
        self.token = self.client_res["token"]
        self.client_id = self.client_res["client"]["id"]

        self.ro_res = self.service.dispatch(
            "client_register",
            {"label": "test-readonly", "scopes": ["read"]},
        )
        self.ro_token = self.ro_res["token"]
        self.ro_client_id = self.ro_res["client"]["id"]

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def test_authenticated_archive_round_trip_shares_event_cursor(self):
        # 1. Prepare a download task and an archive job
        task = DownloadTask("https://fixture.example/archive.zip", str(self.root), id="archive-task")
        self.service.store.save(task)

        archive_path = self.root / "archive.zip"
        archive_path.write_bytes(b"PK\x03\x04fixture-archive")

        job = ArchiveJob(
            "job-1", task.id, str(archive_path), str(self.root / "extracted"),
            package_key="pkg-1", state="queued", operation="extract",
        )
        self.service.store.insert_or_get_archive_job(job)
        ev1 = self.service.events.emit("ArchiveQueued", task.id, {"job_id": "job-1"})

        # 2. Start HTTP server
        server, _ = create_http_server(str(self.root), port=0, token="server-bootstrap-token", service=self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        base_url = f"http://127.0.0.1:{port}"

        try:
            # 2a. Unauthenticated GET /health
            req = urllib.request.Request(f"{base_url}/health")
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)

            # 2b. Unauthenticated POST /rpc returns 401
            req = urllib.request.Request(
                f"{base_url}/rpc",
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "archive_status"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()

            # 2c. POST /rpc with invalid bearer token returns 401
            req = urllib.request.Request(
                f"{base_url}/rpc",
                data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "archive_status"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer bad-token"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()

            # 2d. Read-only token calling mutating archive_cancel returns insufficient scope
            req = urllib.request.Request(
                f"{base_url}/rpc",
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 3, "method": "archive_cancel",
                    "params": {"job_id": "job-1"}, "client_id": self.ro_client_id,
                    "idempotency_key": "ro-cancel-1",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.ro_token}"},
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("error", data)
                self.assertEqual(data["error"]["type"], "PermissionError")
                self.assertIn("insufficient scope", data["error"]["message"].lower())

            # 2e. Unknown method rejected before dispatch
            req = urllib.request.Request(
                f"{base_url}/rpc",
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 4, "method": "unknown_archive_magic",
                    "params": {}, "client_id": self.client_id,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("error", data)
                self.assertEqual(data["error"]["type"], "ValueError")
                self.assertIn("unknown method", data["error"]["message"].lower())

            # 2f. Valid token calls archive_status via HTTP
            req = urllib.request.Request(
                f"{base_url}/rpc",
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 5, "method": "archive_status",
                    "params": {"task_id": task.id}, "client_id": self.client_id,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("result", data)
                jobs = data["result"]
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]["id"], "job-1")

            # 2g. Valid token calls archive_cancel with idempotency_key via HTTP
            req = urllib.request.Request(
                f"{base_url}/rpc",
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 6, "method": "archive_cancel",
                    "params": {"job_id": "job-1"}, "client_id": self.client_id,
                    "idempotency_key": "http-cancel-1",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("result", data)
                self.assertEqual(data["result"]["state"], "canceled")
                cancel_result = data["result"]

            # 2h. Replay archive_cancel with same key and params -> replayed identical result
            req = urllib.request.Request(
                f"{base_url}/rpc",
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 7, "method": "archive_cancel",
                    "params": {"job_id": "job-1"}, "client_id": self.client_id,
                    "idempotency_key": "http-cancel-1",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["result"], cancel_result)

            # 2i. Replay archive_cancel with same key but different params -> rejected
            req = urllib.request.Request(
                f"{base_url}/rpc",
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 8, "method": "archive_cancel",
                    "params": {"job_id": "other-job"}, "client_id": self.client_id,
                    "idempotency_key": "http-cancel-1",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("error", data)
                self.assertEqual(data["error"]["type"], "ValueError")
                self.assertIn("idempotency key was reused", data["error"]["message"])

            # 2j. HTTP events_since queries shared cursor
            req = urllib.request.Request(
                f"{base_url}/rpc",
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 9, "method": "events_since",
                    "params": {"after_id": 0}, "client_id": self.client_id,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                events = data["result"]
                self.assertTrue(any(e["event_type"] == "ArchiveQueued" for e in events))
                self.assertTrue(any(e["event_type"] == "ArchiveCanceled" for e in events))
                ev_to_ack = events[0]["id"]

            # 2k. HTTP event_acknowledge
            req = urllib.request.Request(
                f"{base_url}/rpc",
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 10, "method": "event_acknowledge",
                    "params": {"id": ev_to_ack}, "client_id": self.client_id,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["result"]["acknowledged"], ev_to_ack)

        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # 3. Test stdio serve
        stdio_in = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": "s-0", "method": "archive_status"}) + "\n"  # pre-hello error
            + json.dumps({"jsonrpc": "2.0", "id": "s-1", "method": "hello",
                          "params": {"protocol_version": "v1", "client_id": self.client_id},
                          "auth": {"token": self.token}}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": "s-2", "method": "archive_status",
                          "params": {"task_id": task.id}, "client_id": self.client_id}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": "s-3", "method": "events_since",
                          "params": {"after_id": 0}, "client_id": self.client_id}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": "s-4", "method": "event_acknowledge",
                          "params": {"id": ev1}, "client_id": self.client_id}) + "\n"
        )
        stdio_out = io.StringIO()
        serve(str(self.root), input_stream=stdio_in, output_stream=stdio_out, service=self.service, token=self.token)
        responses = [json.loads(line) for line in stdio_out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(responses), 5)
        # s-0: pre-hello error
        self.assertIn("error", responses[0])
        self.assertIn("stdio hello is required", responses[0]["error"]["message"])
        # s-1: hello_ack
        self.assertIn("result", responses[1])
        self.assertEqual(responses[1]["result"]["type"], "hello_ack")
        self.assertEqual(responses[1]["result"]["client_id"], self.client_id)
        # s-2: archive_status
        self.assertIn("result", responses[2])
        self.assertEqual(responses[2]["result"][0]["id"], "job-1")
        # s-3: events_since
        self.assertIn("result", responses[3])
        self.assertTrue(len(responses[3]["result"]) >= 1)
        # s-4: event_acknowledge
        self.assertIn("result", responses[4])
        self.assertEqual(responses[4]["result"]["acknowledged"], ev1)

        # 4. Test CLI RPC
        archive_req_file = Path("tests/headless/archive_request.json")
        self.assertTrue(archive_req_file.exists())
        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            exit_code = cli_main([
                "--data-dir", str(self.root), "rpc", "archive_status",
                "--params-file", str(archive_req_file),
                "--token", self.token,
            ])
        self.assertEqual(exit_code, 0)
        cli_out = json.loads(out_buf.getvalue())
        self.assertEqual(cli_out["jsonrpc"], "2.0")
        self.assertEqual(cli_out["result"][0]["id"], "job-1")

        # 5. Shared SQLite cursor: check TaskStore acknowledged state
        pending = self.service.events.pending()
        self.assertNotIn(ev_to_ack, [p["id"] for p in pending])

    def test_complete_operation_matrix_scopes_rejections_and_readonly(self):
        """Test every HEAD-01 matrix row for required scope, rejection, and readonly vs mutating."""
        matrix = [
            ("crawl_get", "read", False, {"id": "c-1"}),
            ("crawl_snapshot", "read", False, {"id": "c-1"}),
            ("crawl_list", "read", False, {}),
            ("crawl_status", "read", False, {"id": "c-1"}),
            ("container_import", "enqueue", True, {"content": "https://example.test/1", "format": "text"}),
            ("import_container", "enqueue", True, {"content": "https://example.test/1", "format": "text"}),
            ("crawl_import", "enqueue", True, {"content": "https://example.test/1", "format": "text"}),
            ("media_plan", "enqueue", True, {"url": "https://example.test/manifest.m3u8"}),
            ("media_assemble", "enqueue", True, {"task_id": "m-1"}),
            ("helper_invoke", "worker_control", True, {"helper_id": "test", "operation": "run"}),
            ("helper_status", "worker_control", False, {}),
            ("helper_cancel", "worker_control", True, {"operation_id": "op-1"}),
            ("archive_inspect", "read", False, {"path": "test.zip"}),
            ("archive_submit", "enqueue", True, {"path": str(self.root / "test.zip")}),
            ("archive_status", "read", False, {}),
            ("archive_retry", "enqueue", True, {"job_id": "job-1"}),
            ("archive_cancel", "enqueue", True, {"job_id": "job-1"}),
            ("schedule_get", "read", False, {}),
            ("schedule_set", "account_admin", True, {"id": "q1", "name": "Q1"}),
            ("queue_pause", "account_admin", True, {"id": "default"}),
            ("queue_resume", "account_admin", True, {"id": "default"}),
            ("collection_select", "enqueue", True, {"id": "col-1", "item_ids": []}),
            ("collection_enqueue", "enqueue", True, {"id": "col-1"}),
            ("task_cancel", "enqueue", True, {"id": "t-1"}),
            ("events_since", "read", False, {"after_id": 0}),
            ("event_acknowledge", "read", False, {"id": 1}),
        ]

        # 1. Unknown method rejection before dispatch
        with self.assertRaises(ValueError) as ctx:
            pre_dispatch(self.service, {"jsonrpc": "2.0", "method": "non_existent_rpc_method", "params": {}},
                         {"client_id": self.client_id, "scopes": ["read", "enqueue", "account_admin", "worker_control"]})
        self.assertIn("unknown method", str(ctx.exception).lower())

        # 2. Each row: test insufficient scope rejection and mutating idempotency requirement
        for method, expected_scope, is_mutating, sample_params in matrix:
            self.assertEqual(headless_required_scope(method), expected_scope, f"scope mismatch for {method}")

            # Client with no scopes
            with self.assertRaises(PermissionError, msg=f"{method} should reject client without {expected_scope}"):
                req = {"jsonrpc": "2.0", "method": method, "params": sample_params, "client_id": "no-scope-client"}
                if is_mutating:
                    req["idempotency_key"] = "test-idem-key"
                pre_dispatch(self.service, req, {"client_id": "no-scope-client", "scopes": []})

            # Mutating rows MUST reject missing idempotency_key
            if is_mutating:
                self.assertIn(method, _HEADLESS_MUTATIONS)
                with self.assertRaises(ValueError, msg=f"{method} mutating call must require idempotency_key"):
                    req = {"jsonrpc": "2.0", "method": method, "params": sample_params, "client_id": self.client_id}
                    pre_dispatch(self.service, req, {"client_id": self.client_id, "scopes": [expected_scope]})
            else:
                self.assertNotIn(method, _HEADLESS_MUTATIONS)

    def test_mutating_idempotency_replay_and_mismatch(self):
        queue_params = {"id": "idempotent-q", "name": "Idempotent Q", "priority": 5}
        req = {
            "jsonrpc": "2.0", "method": "schedule_set", "params": queue_params,
            "client_id": self.client_id, "idempotency_key": "q-idem-1",
        }
        first = pre_dispatch(self.service, req, {"client_id": self.client_id, "scopes": ["account_admin"]})
        self.assertTrue(first)

        # Replay with same key and canonical params
        second = pre_dispatch(self.service, req, {"client_id": self.client_id, "scopes": ["account_admin"]})
        self.assertEqual(first, second)

        # Replay with same key but changed params fails
        changed_req = {
            "jsonrpc": "2.0", "method": "schedule_set",
            "params": {"id": "idempotent-q", "name": "Different Name"},
            "client_id": self.client_id, "idempotency_key": "q-idem-1",
        }
        with self.assertRaises(ValueError) as ctx:
            pre_dispatch(self.service, changed_req, {"client_id": self.client_id, "scopes": ["account_admin"]})
        self.assertIn("idempotency key was reused with different parameters", str(ctx.exception))

    def test_forbidden_keys_and_raw_credentials_fail_boundary(self):
        forbidden_samples = [
            {"password": "plain-text-secret"},
            {"secret": "sensitive-data"},
            {"cookie": "session=123"},
            {"token": "raw-auth-token"},
            {"state": "completed"},
            {"lease_id": "fake-lease"},
            {"completed_bytes": 1024},
        ]
        for bad_param in forbidden_samples:
            req = {
                "jsonrpc": "2.0", "method": "schedule_get",
                "params": bad_param, "client_id": self.client_id,
            }
            with self.assertRaises(ValueError, msg=f"Should reject forbidden key in {bad_param}"):
                pre_dispatch(self.service, req, {"client_id": self.client_id, "scopes": ["read"]})

    def test_collection_select_never_expands_parents(self):
        plan = CollectionPlan(
            "col-test", "https://example.test/collection", "generic",
            items=[
                CollectionItem("parent-1", "https://example.test/parent", "parent", "", size=0, selected=False),
                CollectionItem("child-1", "https://example.test/child1", "child1", "parent", size=10, selected=False, parent_id="parent-1"),
                CollectionItem("child-2", "https://example.test/child2", "child2", "parent", size=20, selected=False, parent_id="parent-1"),
            ],
            nodes=[
                {"node_id": "parent-1", "parent_id": None, "source_url": "https://example.test/parent"},
                {"node_id": "child-1", "parent_id": "parent-1", "source_url": "https://example.test/child1"},
                {"node_id": "child-2", "parent_id": "parent-1", "source_url": "https://example.test/child2"},
            ],
        )
        self.service.store.save_collection_plan(plan)

        # Select exact child-1
        req = {
            "jsonrpc": "2.0", "method": "collection_select",
            "params": {"id": "col-test", "item_ids": ["child-1"], "selected": True},
            "client_id": self.client_id, "idempotency_key": "col-select-1",
        }
        res = pre_dispatch(self.service, req, {"client_id": self.client_id, "scopes": ["enqueue"]})
        items_by_id = {it["stable_id"]: it for it in res["items"]}

        # child-1 is selected
        self.assertTrue(items_by_id["child-1"]["selected"])
        # parent-1 was NEVER expanded or selected
        self.assertFalse(items_by_id["parent-1"]["selected"])
        # sibling child-2 remains unselected
        self.assertFalse(items_by_id["child-2"]["selected"])

    def test_cancellation_cannot_reactivate_terminal_jobs(self):
        # 1. Archive job in terminal completed state
        task = DownloadTask("https://example.test/1", str(self.root), id="task-1")
        self.service.store.save(task)

        archive_path = self.root / "completed.zip"
        archive_path.write_bytes(b"PK\x03\x04test")
        job = ArchiveJob(
            "terminal-job", "task-1", str(archive_path), str(self.root),
            package_key="term-pkg", state="completed", operation="extract",
        )
        self.service.store.insert_or_get_archive_job(job)

        cancel_req = {
            "jsonrpc": "2.0", "method": "archive_cancel",
            "params": {"job_id": "terminal-job"},
            "client_id": self.client_id, "idempotency_key": "term-cancel-1",
        }
        res = pre_dispatch(self.service, cancel_req, {"client_id": self.client_id, "scopes": ["enqueue"]})
        self.assertEqual(res["state"], "completed")
        self.assertEqual(self.service.store.get_archive_job("terminal-job").state, "completed")

        # 2. Download task in terminal completed state
        task2 = DownloadTask("https://example.test/file.zip", str(self.root), id="term-task", state="completed")
        self.service.store.save(task2)

        task_cancel_req = {
            "jsonrpc": "2.0", "method": "task_cancel",
            "params": {"id": "term-task"},
            "client_id": self.client_id, "idempotency_key": "task-cancel-1",
        }
        with self.assertRaises(ValueError) as ctx:
            pre_dispatch(self.service, task_cancel_req, {"client_id": self.client_id, "scopes": ["enqueue"]})
        self.assertIn("terminal download state completed cannot transition to canceled", str(ctx.exception))
        self.assertEqual(self.service.store.get("term-task").state, "completed")

    def test_cross_client_events_since_and_acknowledge_ordering(self):
        """Cross-client fixture: verify HTTP /rpc and Native bridge observe identical event IDs, ordering, redacted payloads, and acknowledgements."""
        archive_path = self.root / "cross_test.zip"
        archive_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        task = DownloadTask("https://example.test/cross.zip", str(self.root), id="cross-task", state="completed")
        self.service.store.save(task)

        job = ArchiveJob("cross-job-1", "cross-task", str(archive_path), str(self.root), package_key="cross-pkg", operation="extract")
        self.service.store.insert_or_get_archive_job(job)

        ev1 = self.service.events.emit("ArchiveQueued", "cross-task", {
            "job_id": "cross-job-1", "status": "queued", "secret_token": "SUPER_SECRET_123",
            "source_url": "https://example.test/cross.zip?auth=secret_pass#anchor",
        })
        ev2 = self.service.events.emit("ArchiveCanceled", "cross-task", {
            "job_id": "cross-job-1", "status": "canceled", "reason": "user_canceled",
        })

        # Query events via HTTP /rpc
        server, _ = create_http_server(str(self.root), port=0, token="server-bootstrap-token", service=self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/rpc",
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "events_since",
                    "params": {"after_id": 0}, "client_id": self.client_id,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(req) as resp:
                http_data = json.loads(resp.read().decode("utf-8"))
            http_events = http_data["result"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # Query events via NativeProtocolSession
        native_session = NativeProtocolSession(self.service.dispatch)
        hello_resp = native_session.handle({
            "version": BROWSER_PROTOCOL_VERSION, "type": "hello", "request_id": "h-1",
            "origin": {"extension_origin": "chrome-extension://cross-test", "page_origin": "https://example.test"},
            "capabilities": ["replay"],
        })
        self.assertEqual(hello_resp["type"], "hello_ack")

        native_resp = native_session.handle({
            "version": BROWSER_PROTOCOL_VERSION, "type": "events_since", "request_id": "ev-1",
            "params": {"after_id": 0},
        })
        self.assertEqual(native_resp["type"], "events_since_ack")
        native_events = native_resp["result"]

        # 1. Compare event IDs, types, order, and payloads across HTTP and Native
        self.assertEqual([e["id"] for e in http_events], [e["id"] for e in native_events])
        self.assertEqual([e["event_type"] for e in http_events], [e["event_type"] for e in native_events])
        self.assertEqual([e["payload"] for e in http_events], [e["payload"] for e in native_events])

        # Verify redaction is applied to both
        ev1_http = next(e for e in http_events if e["id"] == ev1)
        ev1_native = next(e for e in native_events if e["id"] == ev1)
        self.assertEqual(ev1_http["payload"]["secret_token"], "[redacted]")
        self.assertEqual(ev1_native["payload"]["secret_token"], "[redacted]")
        self.assertNotIn("secret_pass", ev1_http["payload"]["source_url"])
        self.assertNotIn("secret_pass", ev1_native["payload"]["source_url"])

        # 2. Acknowledge ev1 via Native bridge
        ack_resp = native_session.handle({
            "version": BROWSER_PROTOCOL_VERSION, "type": "acknowledge", "request_id": "ack-1",
            "params": {"id": ev1},
        })
        self.assertEqual(ack_resp["type"], "acknowledge_ack")
        self.assertEqual(ack_resp["status"], "accepted")
        self.assertEqual(ack_resp["result"], {"acknowledged": ev1})

        # Verify acknowledgement effect on shared pending event outbox
        pending_after_native = self.service.events.pending()
        self.assertNotIn(ev1, [p["id"] for p in pending_after_native])
        self.assertIn(ev2, [p["id"] for p in pending_after_native])

        # 3. Native acknowledgement replay is idempotent and returns identical result
        ack_replay = native_session.handle({
            "version": BROWSER_PROTOCOL_VERSION, "type": "acknowledge", "request_id": "ack-1",
            "params": {"id": ev1},
        })
        self.assertEqual(ack_replay["status"], "replayed")
        self.assertEqual(ack_replay["result"], {"acknowledged": ev1})


if __name__ == "__main__":
    unittest.main()
