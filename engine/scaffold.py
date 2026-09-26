from __future__ import annotations

import re
from pathlib import Path


_ID = re.compile(r"[a-z0-9][a-z0-9._-]*$")


def scaffold_plugin(provider_id: str, host: str, output_root: str | Path,
                    implementation: str = "recipe") -> Path:
    if not _ID.fullmatch(provider_id):
        raise ValueError("provider id must contain only lowercase letters, numbers, dots, underscores, or hyphens")
    if not host or any(char in host for char in " /\\\t\r\n"):
        raise ValueError("host must be a hostname")
    if implementation not in {"recipe", "python"}:
        raise ValueError("implementation must be recipe or python")
    directory = Path(output_root).resolve() / provider_id
    if directory.exists():
        raise FileExistsError(f"plugin directory already exists: {directory}")
    directory.joinpath("fixtures").mkdir(parents=True)
    directory.joinpath("tests").mkdir()
    manifest = f'''{{
  // Generated built-in provider. Keep provider logic in recipe.jsonc when possible.
  "schema_version": 2,
  "implementation": "{implementation}",
  "id": "{provider_id}",
  "version": "0.1.0",
  "display_name": "{provider_id}",
  "hosts": ["{host}"],
  "match": {{ "schemes": ["https"], "path_regex": ".*" }},
  "capabilities": {{ "http": true, "metadata": true, "refresh": true }},
  "permissions": {{ "hosts": ["{host}"], "secrets": [] }},
  "limits": {{ "max_concurrent_items": 2, "requests_per_second": 4 }},
  {('"recipe": "recipe.jsonc"' if implementation == 'recipe' else '"hooks": {"resolve": "resolve", "refresh": "refresh"}')}
}}
'''
    recipe = '''{
  // Safe JSONPath/CSS extraction only; arbitrary code is not supported.
  "limits": { "response_bytes": 4194304, "redirects": 8, "pages": 32, "items": 10000 },
  "operations": {
    "resolve": {
      "request": { "method": "GET", "url": "{input_url}" },
      "extract": {
        "fields": {
          "name": { "header": "Content-Disposition", "default": "download" },
          "url": "{input_url}",
          "size": { "header": "Content-Length" }
        }
      }
    }
  },
  "errors": [
    { "category": "not_found", "status": [404] },
    { "category": "retryable", "status": [408, 429, 500, 502, 503, 504], "retry_after": true }
  ]
}
'''
    hooks = '''"""Custom Python hook placeholder.

Recipe plugins do not need hooks.py. Use implementation=python only for
cryptography, complex authentication, browser flows, or unusual protocols.
"""

from engine.providers.generic import GenericProvider


def resolve(params):
    return [item.to_dict() for item in GenericProvider.resolve(params["url"], params.get("secrets"))]


def refresh(params):
    return resolve({"url": params["item"]["source_url"], "secrets": params.get("secrets")})
'''
    class_name = re.sub(r"[^A-Za-z0-9]", "_", provider_id).title() + "ContractTests"
    if class_name[0].isdigit():
        class_name = "Provider" + class_name
    contract = f'''import unittest
from pathlib import Path

from engine.plugin_catalog import load_manifest
from engine.recipe_runtime import load_recipe


class {class_name}(unittest.TestCase):
    plugin_dir = Path(__file__).parents[1]

    def test_manifest_and_recipe_contract(self):
        manifest = load_manifest(self.plugin_dir)
        self.assertEqual(manifest["id"], "{provider_id}")
        if manifest.get("implementation") == "recipe":
            recipe = load_recipe(self.plugin_dir, manifest)
            self.assertIn("resolve", recipe["operations"])


if __name__ == "__main__":
    unittest.main()
'''
    readme = f'''# {provider_id}

Built-in provider scaffold for `{host}`.

Start with `manifest.jsonc` matching and permissions. Put REST/HTML extraction,
pagination, refresh, and error mappings in `recipe.jsonc`. Use Python only for
cryptography, complex authentication, browser flows, or unusual protocols.

Run the provider contract tests after replacing the sample request and fixture.
Never place passwords, cookies, access tokens, or signed URLs in manifests,
fixtures, logs, or committed test output.
'''
    files = {
        "manifest.jsonc": manifest,
        "recipe.jsonc": recipe,
        "hooks.py": hooks,
        "fixtures/sample-response.json": '{\n  "data": { "name": "example.bin", "size": 0, "url": "https://cdn.example.invalid/example.bin" }\n}\n',
        "fixtures/sample-page.html": '<html><body><a class="download" href="/example.bin">Download</a></body></html>\n',
        "tests/test_contract.py": contract,
        "README.md": readme,
    }
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return directory
