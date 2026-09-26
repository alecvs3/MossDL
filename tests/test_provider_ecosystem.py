from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from engine.plugin_catalog import ManifestError, validate_manifest
from engine.plugin_worker import main as worker_main
import engine.provider_sdk as provider_sdk
from engine.plugins import PluginProcess, PluginRegistry
from engine.provider_sdk import (
    ProviderContractError,
    ProviderOperationRequest,
    catalog_entry_digest,
    declared_operations,
    replay_fixture,
    sign_catalog,
    validate_catalog_entry,
    verify_catalog,
)


class ProviderEcosystemTests(unittest.TestCase):
    def _plugin(self, *, operation: str = "enumerate", result: str | None = None) -> Path:
        parent = Path(tempfile.mkdtemp(prefix="provider-fixture-"))
        root = parent / "fixture"
        root.mkdir()
        result = result or "[{\"provider\": \"fixture\", \"source_url\": \"https://fixture.test/file\", \"display_name\": \"file\", \"item_id\": \"file-1\", \"parent_id\": \"root\"}]"
        operation_names = ["resolve"] if operation == "resolve" else ["resolve", operation]
        operations_json = json.dumps(operation_names)
        hooks_json = json.dumps({"resolve": "resolve", operation: operation})
        (root / "manifest.jsonc").write_text(textwrap.dedent(f"""
            {{
              "schema_version": 2, "protocol_version": 2, "id": "fixture", "version": "1.0.0",
              "roles": ["resolver"], "hosts": ["fixture.test"],
              "match": {{"schemes": ["https"]}},
              "capabilities": {{"folders": true}}, "operations": {operations_json},
              "limits": {{"max_concurrent_items": 1}},
              "permissions": {{"hosts": ["fixture.test"]}},
              "hooks": {hooks_json}
            }}
        """).strip(), encoding="utf-8")
        operation_body = result if result.startswith("raise ") else f"return {result}"
        (root / "hooks.py").write_text(textwrap.dedent(f"""
            def resolve(params):
                return [{{"provider": "fixture", "source_url": params["url"], "display_name": "legacy"}}]

            def {operation}(params):
                {operation_body}
        """).strip(), encoding="utf-8")
        return root

    def test_fixture_operation_crosses_isolated_worker_and_preserves_resolved_shape(self):
        plugin = self._plugin()
        process = PluginProcess(plugin)
        try:
            result = process.call("enumerate", {"url": "https://fixture.test/root"})
        finally:
            process.close()
        self.assertEqual(result[0]["item_id"], "file-1")
        self.assertEqual(result[0]["parent_id"], "root")
        self.assertEqual(set(result[0]) - {"provider", "source_url", "display_name", "item_id", "parent_id"}, set())

    def test_registry_keeps_legacy_resolve_and_declared_operation_is_typed(self):
        plugin = self._plugin()
        registry = PluginRegistry(external_dirs=[plugin.parent])
        try:
            self.assertEqual(registry.resolve("https://fixture.test/root")[0].display_name, "legacy")
            self.assertTrue(registry.has_operation("fixture", "enumerate"))
            result = registry.call_capability("fixture", "enumerate", {"url": "https://fixture.test/root"})
            self.assertEqual(result.operation, "enumerate")
            self.assertEqual(result.value[0]["parent_id"], "root")
        finally:
            registry.close()

    def test_operation_envelope_rejects_engine_authority_and_undeclared_methods(self):
        with self.assertRaises(ProviderContractError):
            replay_fixture({"enumerate": {"result": []}}, "enumerate", {"engine_state": "downloading"}, declared={"enumerate"})
        with self.assertRaises(ProviderContractError):
            replay_fixture({"operations": {}}, "helper", {}, declared={"enumerate"})

    def test_signed_catalog_digest_and_allowlists_fail_closed(self):
        entry = {"id": "fixture", "hosts": ["fixture.test"], "operations": ["enumerate"]}
        entry["digest"] = catalog_entry_digest(entry)
        validate_catalog_entry(entry, allow_hosts={"fixture.test"}, allow_operations={"enumerate"})
        with self.assertRaises(ProviderContractError):
            validate_catalog_entry({**entry, "hosts": ["evil.test"]}, allow_hosts={"fixture.test"})
        signed = sign_catalog({"providers": [entry]}, "test-secret")
        altered = {**signed, "payload": signed["payload"][:-2] + "AA"}
        with self.assertRaises(ValueError):
            verify_catalog(altered, "test-secret")

    def test_manifest_v1_remains_valid_and_unknown_operation_is_rejected(self):
        manifest = {
            "schema_version": 1, "id": "legacy", "version": "1", "hosts": ["legacy.test"],
            "match": {}, "capabilities": {}, "limits": {}, "hooks": {"resolve": "resolve"},
        }
        self.assertEqual(validate_manifest(manifest)["id"], "legacy")
        with self.assertRaises(ManifestError):
            validate_manifest({**manifest, "operations": ["not-a-provider-operation"]})

    def test_replay_is_redacted_and_typed(self):
        request = ProviderOperationRequest("fixture", "metadata", {"url": "https://fixture.test/a?token=raw"})
        result = replay_fixture({"metadata": {"request": {"url": "https://fixture.test/a"}, "result": {"title": "A"}}}, "metadata", {"url": "https://fixture.test/a"}, declared={"metadata"})
        self.assertEqual(request.to_dict()["params"]["url"], "https://fixture.test/a")
        self.assertEqual(result.to_dict()["value"]["title"], "A")

    def test_all_new_operation_families_have_replayable_typed_results(self):
        cases = {
            "enumerate": [{"provider": "fixture", "source_url": "https://fixture.test/a", "display_name": "A"}],
            "extract_links": [{"provider": "fixture", "source_url": "https://fixture.test/a", "display_name": "A"}],
            "metadata": {"title": "A", "duration": 3},
            "authenticate": {"account_ref": "opaque-account"},
            "refresh": [{"provider": "fixture", "source_url": "https://fixture.test/a", "display_name": "A"}],
            "helper": {"status": "completed", "progress": 1.0},
            "health": {"healthy": True},
        }
        fixture = {name: {"result": value} for name, value in cases.items()}
        for operation in cases:
            with self.subTest(operation=operation):
                result = replay_fixture(fixture, operation, {}, declared=set(cases))
                self.assertEqual(result.operation, operation)

    def test_malformed_worker_result_fails_closed(self):
        plugin = self._plugin(operation="metadata", result="[1, 2, 3]")
        process = PluginProcess(plugin)
        try:
            with self.assertRaises(Exception) as raised:
                process.call("metadata", {"url": "https://fixture.test/root"})
            self.assertIn("result must be an object", str(raised.exception))
        finally:
            process.close()

    def test_repeated_plugin_faults_quarantine_and_block_new_calls(self):
        plugin = self._plugin(operation="resolve", result="raise RuntimeError('broken provider')")
        registry = PluginRegistry(external_dirs=[plugin.parent])
        try:
            for _ in range(3):
                with self.assertRaises(Exception):
                    registry.call_capability("fixture", "resolve", {"url": "https://fixture.test/root"})
            self.assertTrue(registry.health["fixture"]["quarantined"])
            with self.assertRaises(Exception) as raised:
                registry.call_capability("fixture", "resolve", {"url": "https://fixture.test/root"})
            self.assertIn("unavailable", str(raised.exception))
        finally:
            registry.close()

    def test_signed_catalog_admission_requires_signature_and_entry_digests(self):
        self.assertTrue(hasattr(provider_sdk, "validate_signed_catalog"))
        entry = {"id": "fixture", "hosts": ["fixture.test"], "operations": ["enumerate"]}
        with self.assertRaises(ProviderContractError):
            provider_sdk.validate_signed_catalog({"providers": [entry]}, "test-secret",
                                                 allow_hosts={"fixture.test"}, allow_operations={"enumerate"})


if __name__ == "__main__":
    unittest.main()
