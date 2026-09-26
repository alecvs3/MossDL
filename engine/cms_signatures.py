"""CMS signature detection and declarative recipe runner for cyberlockers.

Detects common cyberlocker software scripts (XFileSharing, YetiShare, etc.)
and executes declarative JSONC recipes to extract direct download links.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .plugin_catalog import loads_jsonc
from .recipe_runtime import RecipeRuntime, _Response, validate_recipe


RECIPES_DIR = Path(__file__).resolve().parent / "recipes"


# Heuristic patterns for recognizing popular cyberlocker CMS engines from HTML
CMS_SIGNATURES: list[dict[str, Any]] = [
    {
        "cms_id": "xfilesharing",
        "recipe_file": "xfilesharing.recipe.jsonc",
        "patterns": [
            re.compile(r"name=[\"']op[\"']\s+value=[\"']download2[\"']", re.I),
            re.compile(r"name=[\"']fname[\"']", re.I),
            re.compile(r"xfilesharing", re.I),
            re.compile(r"sibsoft", re.I),
        ],
        "min_matches": 2,
    },
    {
        "cms_id": "yetishare",
        "recipe_file": "yetishare.recipe.jsonc",
        "patterns": [
            re.compile(r"btn-free-download", re.I),
            re.compile(r"name=[\"']fileId[\"']", re.I),
            re.compile(r"yetishare", re.I),
            re.compile(r"mfp-inline", re.I),
        ],
        "min_matches": 2,
    },
]


class CMSRecipeEngine:
    """Evaluates cyberlocker CMS engines against page HTML and runs matching recipes."""

    def __init__(self, transport: Callable[..., _Response] | None = None) -> None:
        self.transport = transport
        self._cached_recipes: dict[str, dict[str, Any]] = {}
        self._load_recipes()

    def _load_recipes(self) -> None:
        if not RECIPES_DIR.is_dir():
            return
        for sig in CMS_SIGNATURES:
            recipe_path = RECIPES_DIR / sig["recipe_file"]
            if recipe_path.is_file():
                try:
                    data = loads_jsonc(recipe_path.read_text(encoding="utf-8"))
                    self._cached_recipes[sig["cms_id"]] = validate_recipe(data)
                except Exception:
                    pass

    def detect(self, html_text: str) -> str | None:
        """Analyze page HTML text to detect if it matches a known CMS."""
        if not html_text:
            return None
        for sig in CMS_SIGNATURES:
            matches = sum(1 for pattern in sig["patterns"] if pattern.search(html_text))
            if matches >= sig["min_matches"]:
                return sig["cms_id"]
        return None

    def resolve(self, cms_id: str, url: str, page_html: str | None = None) -> list[dict[str, Any]]:
        """Execute the recipe for the detected CMS."""
        recipe_data = self._cached_recipes.get(cms_id)
        if not recipe_data:
            return []

        manifest = {
            "id": f"cms.{cms_id}",
            "version": "1.0.0",
            "recipe": f"{cms_id}.recipe.jsonc",
            "permissions": {"hosts": ["*"], "secrets": []},
        }

        # If page_html is provided and no custom transport, provide a transport
        # that returns the initial page_html on the first request if matching url.
        transport = self.transport
        if transport is None and page_html is not None:
            from email.message import Message

            def _initial_transport(request, timeout, _redirects):
                req_url = request.get_full_url() if hasattr(request, "get_full_url") else str(request)
                headers = Message()
                headers["Content-Type"] = "text/html; charset=utf-8"
                return _Response(req_url, 200, headers, page_html.encode("utf-8"))

            transport = _initial_transport

        runtime = RecipeRuntime(RECIPES_DIR, manifest, transport=transport)
        # Override recipe in runtime directly
        runtime.recipe = recipe_data

        try:
            items = runtime.call("resolve", {"url": url})
            return items if isinstance(items, list) else []
        except Exception:
            return []
