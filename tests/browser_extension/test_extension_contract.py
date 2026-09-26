import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_browser_extension import (
    PROTOCOL_VERSION,
    build_common_extension,
    build_extension,
    check_build,
    validate_common_sources,
)
from engine.browser_bridge import NativeProtocolSession, PROTOCOL_VERSION


class BrowserExtensionContractTests(unittest.TestCase):
    def test_common_sources_share_versioned_protocol_and_safety_contract(self):
        contract = validate_common_sources()
        self.assertEqual(contract["protocol_version"], PROTOCOL_VERSION)
        with tempfile.TemporaryDirectory() as raw:
            result = build_common_extension(Path(raw) / "common-artifact")
            common = Path(result["output"]) / "common"
            self.assertEqual(sorted(path.name for path in common.iterdir()),
                             ["background.js", "browserApi.js", "content.js", "popup.js", "protocol.js"])
            protocol = (common / "protocol.js").read_text(encoding="utf-8")
            self.assertIn("browser-capture/1", protocol)
            self.assertIn("ReplayQueue", protocol)
            self.assertIn("authorization", protocol)
            self.assertIn("isEligibleRequest", protocol)

    def test_build_is_deterministic_and_emits_three_adapter_targets(self):
        with tempfile.TemporaryDirectory() as raw:
            first = Path(raw) / "one"
            second = Path(raw) / "two"
            build_extension(first, check=True)
            build_extension(second, check=True)
            self.assertEqual(check_build(first)["protocol_version"], PROTOCOL_VERSION)
            for browser in ("chrome", "edge", "firefox"):
                first_manifest = json.loads((first / browser / "manifest.json").read_text(encoding="utf-8"))
                second_manifest = json.loads((second / browser / "manifest.json").read_text(encoding="utf-8"))
                first_config = json.loads((first / browser / "config.json").read_text(encoding="utf-8"))
                second_config = json.loads((second / browser / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(first_manifest, second_manifest)
                self.assertEqual(first_config, second_config)
                self.assertEqual(first_config["protocol_version"], PROTOCOL_VERSION)
                self.assertEqual(first_manifest["manifest_version"], 3)
                self.assertIn("nativeMessaging", first_manifest["permissions"])

    def test_common_behavior_is_copied_byte_for_byte_to_browser_outputs(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "extension"
            build_extension(output)
            for name in ("protocol.js", "browserApi.js", "background.js", "content.js", "popup.js"):
                values = [(output / browser / name).read_bytes() for browser in ("chrome", "edge", "firefox")]
                self.assertEqual(values[0], values[1])
                self.assertEqual(values[1], values[2])

    def test_native_acknowledgement_replay_does_not_dispatch_the_same_batch_twice(self):
        calls = []
        session = NativeProtocolSession(lambda method, params: calls.append((method, params["batch_id"])) or {"accepted": True})
        hello = {"version": PROTOCOL_VERSION, "type": "hello", "request_id": "hello-1",
                 "origin": {"extension_origin": "chrome-extension://fixture", "page_origin": "https://example.test"},
                 "capabilities": ["replay"]}
        batch = {"version": PROTOCOL_VERSION, "type": "candidate_batch", "request_id": "request-1", "batch_id": "batch-1",
                 "origin": {"extension_origin": "chrome-extension://fixture", "page_origin": "https://example.test"},
                 "page": {"url": "https://example.test/page"}, "candidates": [{"url": "https://cdn.example.test/a.bin"}]}
        session.handle(hello)
        first = session.handle(batch)
        replay = session.handle(batch)
        self.assertEqual(calls, [("browser_capture_batch", "batch-1")])
        self.assertEqual(first["result"], replay["result"])
        self.assertEqual(replay["status"], "replayed")


if __name__ == "__main__":
    unittest.main()
