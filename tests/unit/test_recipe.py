from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import shutil
import unittest
from contextlib import contextmanager
from email.message import Message
from pathlib import Path

from engine.plugin_catalog import loads_jsonc, validate_manifest
from engine.recipe_runtime import RecipeRuntime, _Response, validate_recipe
from engine.scaffold import scaffold_plugin
from engine.plugins import PluginRegistry
from engine.errors import NeedsUser, ProviderMappedError


def response(url: str, payload, status: int = 200, content_type: str = "application/json") -> _Response:
    headers = Message()
    headers["Content-Type"] = content_type
    return _Response(url, status, headers, payload if isinstance(payload, bytes) else json.dumps(payload).encode())


class RecipeTests(unittest.TestCase):
    @contextmanager
    def _temporary_root(self):
        # The managed Windows Python runtime may not be allowed to create child
        # directories below the OS temp directory. Keep test-only fixtures in
        # the repository's existing artifact area instead.
        root = Path(__file__).resolve().parent / ".test-artifacts" / "recipe-runtime" / self._testMethodName
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        try:
            yield root
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _plugin(self, root: Path, recipe: dict, *, folders: bool = False) -> tuple[Path, dict]:
        plugin = root / "example.test"
        plugin.mkdir(parents=True)
        manifest = {
            "schema_version": 2,
            "implementation": "recipe",
            "id": "example.test",
            "version": "1.0.0",
            "hosts": ["example.test"],
            "match": {"schemes": ["https"], "path_regex": ".*"},
            "capabilities": {"http": True, "folders": folders, "refresh": True},
            "permissions": {"hosts": ["example.test", "api.example.test"], "secrets": ["token"]},
            "limits": {"max_concurrent_items": 2},
            "recipe": "recipe.jsonc",
        }
        (plugin / "manifest.jsonc").write_text(json.dumps(manifest), encoding="utf-8")
        (plugin / "recipe.jsonc").write_text(json.dumps(recipe), encoding="utf-8")
        return plugin, manifest

    def test_recipe_resolves_json_and_enumerates_without_direct_urls(self):
        recipe = {"operations": {
            "resolve": {"request": {"url": "https://api.example.test/file/{url.path.0}"}, "extract": {"fields": {
                "name": "$.data.name", "item_id": "$.data.id", "url": "$.data.url", "size": "$.data.size"}}},
            "enumerate": {"request": {"url": "https://api.example.test/tree"}, "extract": {"items": "$.data.items[*]", "fields": {
                "name": "$.name", "item_id": "$.id", "type": "$.type", "parent_id": "$.parent", "path": "$.path", "size": "$.size"}}},
        }}
        with self._temporary_root() as temporary:
            plugin, manifest = self._plugin(temporary, recipe, folders=True)
            def transport(request, _timeout, _redirects):
                if request.full_url.endswith("/tree"):
                    return response(request.full_url, {"data": {"items": [
                        {"id": "root", "name": "root", "type": "folder", "path": "root"},
                        {"id": "file", "name": "one.bin", "type": "file", "parent": "root", "path": "root/one.bin", "size": 12},
                    ]}})
                return response(request.full_url, {"data": {"id": "file", "name": "one.bin", "url": "https://cdn.example.test/one.bin", "size": 12}})
            runtime = RecipeRuntime(plugin, manifest, transport)
            tree = runtime.call("enumerate", {"url": "https://example.test/file/demo", "secrets": {}})
            items = runtime.call("resolve", {"url": "https://example.test/file/demo", "secrets": {}})
        self.assertEqual(tree[1]["metadata"]["parent_id"], "root")
        self.assertIsNone(tree[1]["direct_url"])
        self.assertEqual(items[0]["direct_url"], "https://cdn.example.test/one.bin")
        self.assertEqual(items[0]["size"], 12)

    def test_recipe_error_mapping_and_forbidden_code(self):
        recipe = {"operations": {"resolve": {"request": {"url": "https://example.test/file"}}},
                  "errors": [{"category": "password_required", "status": [403]}]}
        with self._temporary_root() as temporary:
            plugin, manifest = self._plugin(temporary, recipe)
            runtime = RecipeRuntime(plugin, manifest, lambda request, _timeout, _redirects: response(request.full_url, {"error": "password"}, 403))
            with self.assertRaises(NeedsUser):
                runtime.call("resolve", {"url": "https://example.test/file", "secrets": {}})
            mapped = {"operations": {"resolve": {"request": {"url": "https://example.test/file"}}},
                      "errors": [{"category": "retryable", "status": [503], "retry_after": True}]}
            plugin2, manifest2 = self._plugin(temporary / "mapped", mapped)
            retry_runtime = RecipeRuntime(plugin2, manifest2,
                                           lambda request, _timeout, _redirects: response(request.full_url, {}, 503))
            with self.assertRaises(ProviderMappedError) as raised:
                retry_runtime.call("resolve", {"url": "https://example.test/file", "secrets": {}})
            self.assertEqual(raised.exception.category, "retryable")
            with self.assertRaises(Exception):
                validate_recipe({"operations": {"resolve": {"script": "bad"}}})

    def test_recipe_pagination_and_path_safety_are_bounded(self):
        recipe = {"limits": {"pages": 4}, "operations": {
            "enumerate": {"request": {"url": "https://api.example.test/tree",
                                         "pagination": {"next": "$.next", "max_pages": 4}},
                           "extract": {"items": "$.items[*]", "fields": {
                               "name": "$.name", "item_id": "$.id", "path": "$.path", "type": "file"}}}
        }}
        with self._temporary_root() as temporary:
            plugin, manifest = self._plugin(temporary, recipe, folders=True)
            def transport(request, _timeout, _redirects):
                page = "2" if "page=2" in request.full_url else "1"
                payload = {"items": [{"id": "file-" + page, "name": page + ".bin", "path": page + ".bin"}],
                           "next": "https://api.example.test/tree?page=2" if page == "1" else None}
                return response(request.full_url, payload)
            runtime = RecipeRuntime(plugin, manifest, transport)
            items = runtime.call("enumerate", {"url": "https://example.test/tree", "secrets": {}})
            self.assertEqual([item["item_id"] for item in items], ["file-1", "file-2"])
            bad = {"operations": {"resolve": {"request": {"url": "https://example.test/file"},
                                                  "extract": {"fields": {"name": "$.name", "path": "../escape"}}}}}
            bad_plugin, bad_manifest = self._plugin(temporary / "bad", bad)
            with self.assertRaises(Exception):
                RecipeRuntime(bad_plugin, bad_manifest, transport).call(
                    "resolve", {"url": "https://example.test/file", "secrets": {}})

    def test_scaffold_creates_valid_recipe_plugin_and_inspection_is_capability_driven(self):
        with self._temporary_root() as root:
            directory = scaffold_plugin("newsite", "newsite.example", root)
            manifest = validate_manifest(loads_jsonc((directory / "manifest.jsonc").read_text(encoding="utf-8")))
            self.assertEqual(manifest["implementation"], "recipe")
            registry = PluginRegistry(external_dirs=[root])
            try:
                info = registry.inspect_url("https://newsite.example/file")
            finally:
                registry.close()
        self.assertEqual(info["provider_id"], "newsite")
        self.assertEqual(info["ui_mode"], "download")


if __name__ == "__main__":
    unittest.main()
