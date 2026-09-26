from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from engine.errors import ProviderUnavailable
from engine.models import ResolvedItem


PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "datanodes"


def _load_hooks():
    sys.path.insert(0, str(PLUGIN_DIR))
    try:
        spec = importlib.util.spec_from_file_location("datanodes_test_hooks", PLUGIN_DIR / "hooks.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class DataNodesPluginPrimaryTests(unittest.TestCase):
    def test_primary_plugin_is_used_without_touching_legacy_provider(self) -> None:
        hooks = _load_hooks()
        item = ResolvedItem("datanodes", "https://datanodes.to/a/f.rar", "f.rar", "f.rar",
                            direct_url="https://dlproxy.uk/f.rar")
        with patch.object(hooks, "resolve_primary", return_value=[item]) as primary, \
             patch.object(hooks.LegacyDatanodesProvider, "resolve") as legacy:
            result = hooks.resolve({"url": item.source_url, "secrets": {}})
        self.assertEqual(result[0]["direct_url"], item.direct_url)
        primary.assert_called_once()
        legacy.assert_not_called()

    def test_only_explicit_unsupported_shape_enters_logged_fallback(self) -> None:
        hooks = _load_hooks()
        item = ResolvedItem("datanodes", "https://datanodes.to/a/f.rar", "f.rar", "f.rar",
                            direct_url="https://dlproxy.uk/f.rar")
        with patch.object(hooks, "resolve_primary", side_effect=hooks.PrimaryFlowUnsupported("shape")), \
             patch.object(hooks.LegacyDatanodesProvider, "resolve", return_value=[item]) as legacy, \
             patch.object(hooks.telemetry_bus, "record") as record:
            hooks.resolve({"url": item.source_url, "secrets": {"task_id": "task-1"}})
        legacy.assert_called_once()
        self.assertIn("DATANODES_PRIMARY_FALLBACK", record.call_args.kwargs["message"])

    def test_runtime_provider_failure_does_not_cascade_to_legacy(self) -> None:
        hooks = _load_hooks()
        with patch.object(hooks, "resolve_primary", side_effect=ProviderUnavailable("offline")), \
             patch.object(hooks.LegacyDatanodesProvider, "resolve") as legacy:
            with self.assertRaises(ProviderUnavailable):
                hooks.resolve({"url": "https://datanodes.to/a/f.rar", "secrets": {}})
        legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
