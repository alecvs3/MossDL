import io
import json
import struct
import unittest

from engine.browser_bridge import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    NativeProtocolSession,
    ProtocolError,
    read_message,
    serve_native,
    write_message,
)


def read_frames(stream: io.BytesIO):
    stream.seek(0)
    result = []
    while True:
        value = read_message(stream)
        if value is None:
            return result
        result.append(value)


class BrowserCaptureProtocolTests(unittest.TestCase):
    def hello(self):
        return {
            "version": PROTOCOL_VERSION,
            "type": "hello",
            "request_id": "req-hello",
            "origin": {"extension_origin": "chrome-extension://fixture", "page_origin": "https://example.test"},
            "capabilities": ["candidate-batches", "replay"],
        }

    def batch(self, request_id="req-batch"):
        return {
            "version": PROTOCOL_VERSION,
            "type": "candidate_batch",
            "request_id": request_id,
            "batch_id": "batch-1",
            "origin": {"extension_origin": "chrome-extension://fixture", "page_origin": "https://example.test"},
            "page": {"url": "https://example.test/page"},
            "candidates": [{"url": "https://cdn.example.test/file.bin", "method": "GET"}],
        }

    def test_handshake_and_batch_ack_use_stable_identity(self):
        calls = []
        protocol = NativeProtocolSession(lambda method, params: calls.append((method, params)) or {"batch_id": params["batch_id"]})

        hello = protocol.handle(self.hello())
        ack = protocol.handle(self.batch())

        self.assertEqual(hello["type"], "hello_ack")
        self.assertEqual(protocol.state, "ready")
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(ack["batch_id"], "batch-1")
        self.assertEqual(calls[0][0], "browser_capture_batch")
        self.assertEqual(calls[0][1]["request_id"], "req-batch")

    def test_handshake_is_required_and_versions_are_strict(self):
        protocol = NativeProtocolSession(lambda *_: None)
        with self.assertRaisesRegex(ProtocolError, "unsupported browser capture"):
            protocol.handle({**self.hello(), "version": "browser-capture/999"})
        with self.assertRaisesRegex(ProtocolError, "handshake"):
            protocol.handle(self.batch())

    def test_framed_native_stream_dispatches_and_returns_structured_error(self):
        incoming, outgoing = io.BytesIO(), io.BytesIO()
        write_message(incoming, self.hello())
        write_message(incoming, self.batch())
        incoming.seek(0)
        service_calls = []
        service = type("Service", (), {
            "dispatch": lambda _self, method, params: service_calls.append((method, params["batch_id"])) or {"accepted": True},
        })()

        serve_native("unused", input_stream=incoming, output_stream=outgoing, service=service)
        frames = read_frames(outgoing)
        self.assertEqual([item["type"] for item in frames], ["hello_ack", "ack"])
        self.assertEqual(service_calls, [("browser_capture_batch", "batch-1")])
        self.assertEqual(frames[1]["result"], {"accepted": True})

    def test_native_status_events_since_and_acknowledge(self):
        calls = []
        events_fixture = [{"id": 1, "event_id": "1", "event_type": "ArchiveQueued", "payload": {}}]
        dispatch_map = {
            "api_info": lambda params: {"version": "2.2.0", "engine": "ok"},
            "list_events": lambda params: events_fixture,
            "ack_event": lambda params: {"acknowledged": params.get("id") or params.get("event_ids")},
        }
        protocol = NativeProtocolSession(lambda method, params: calls.append((method, params)) or dispatch_map[method](params))
        protocol.handle(self.hello())

        status_resp = protocol.handle({"version": PROTOCOL_VERSION, "type": "status", "request_id": "req-status", "params": {}})
        self.assertEqual(status_resp["type"], "status_ack")
        self.assertEqual(status_resp["result"]["engine"], "ok")
        self.assertEqual(calls[0][0], "api_info")

        events_resp = protocol.handle({"version": PROTOCOL_VERSION, "type": "events_since", "request_id": "req-events", "params": {"after_id": 0}})
        self.assertEqual(events_resp["type"], "events_since_ack")
        self.assertEqual(events_resp["result"], events_fixture)
        self.assertEqual(calls[1][0], "list_events")
        self.assertEqual(calls[1][1]["after_id"], 0)

        ack_resp = protocol.handle({"version": PROTOCOL_VERSION, "type": "acknowledge", "request_id": "req-ack-1", "params": {"id": 1}})
        self.assertEqual(ack_resp["type"], "acknowledge_ack")
        self.assertEqual(ack_resp["status"], "accepted")
        self.assertEqual(ack_resp["result"], {"acknowledged": 1})
        self.assertEqual(calls[2][0], "ack_event")

        # Replay same acknowledge request_id
        ack_replay = protocol.handle({"version": PROTOCOL_VERSION, "type": "acknowledge", "request_id": "req-ack-1", "params": {"id": 1}})
        self.assertEqual(ack_replay["status"], "replayed")
        self.assertEqual(ack_replay["result"], {"acknowledged": 1})
        self.assertEqual(len(calls), 3)  # Did not dispatch ack_event again

    def test_large_and_truncated_frames_are_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "frame limit"):
            read_message(io.BytesIO(struct.pack("<I", MAX_FRAME_BYTES + 1)))
        with self.assertRaisesRegex(ValueError, "truncated native-messaging payload"):
            read_message(io.BytesIO(struct.pack("<I", 10) + b"{}"))


class CaptureFileKindTests(unittest.TestCase):
    def test_batches_are_tagged_with_file_kind_when_read(self):
        from engine.capture import with_file_kinds

        batch = {"candidates": [
            {"url": "https://cdn.test/v", "mime": "video/mp4"},
            {"url": "https://cdn.test/pack.part2.rar"},
            {"url": "https://cdn.test/x", "file_kind": "subtitles"},
        ]}
        kinds = [c["file_kind"] for c in with_file_kinds(batch)["candidates"]]
        self.assertEqual(kinds, ["video", "archives", "subtitles"])

    def test_infrastructure_requests_are_marked_as_noise(self):
        from engine.capture import noise_reason

        self.assertEqual(noise_reason("https://brunhild.challenges.cloudflare.com/x"), "site infrastructure")
        self.assertEqual(noise_reason("https://rutracker.org/cdn-cgi/challenge-platform/h/b"), "bot challenge")
        self.assertIsNone(noise_reason("https://rutrk.org/1.mp4"))


if __name__ == "__main__":
    unittest.main()
