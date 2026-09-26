"""Ad and tracker blocking on the filter lists uBlock Origin uses.

The lists live in the data directory and are compiled by the transfer core's
adblock engine (Brave's). One engine serves the page crawl (ad links never
become candidates), capture review (ad requests are noise) and the browser
(ad requests are dropped and ad elements hidden). Until the full lists are
fetched (on the active route, refreshed weekly), the app's own ad-domain list
is compiled instead, so blocking works offline from the first run.
"""
from __future__ import annotations

import threading
import urllib.request
import time
from pathlib import Path
from typing import Any

from . import route_http, rust_session
from .telemetry import telemetry_bus

FULL_LISTS = {
    "easylist": "https://easylist.to/easylist/easylist.txt",
    "easyprivacy": "https://easylist.to/easylist/easyprivacy.txt",
    "ublock-filters": "https://ublockorigin.github.io/uAssets/filters/filters.txt",
    "ublock-badware": "https://ublockorigin.github.io/uAssets/filters/badware.txt",
    "peter-lowe": "https://pgl.yoyo.org/adservers/serverlist.php?hostformat=adblockplus&showintro=0&mimetype=plaintext",
}
REFRESH_AFTER = 7 * 24 * 3600
_MAX_LIST_BYTES = 16 * 1024 * 1024
USER_AGENT = "MossDL/0.1 (filter list update; +https://github.com/alecvs3/MossDL)"


def _record(level: str, message: str, **context: Any) -> None:
    telemetry_bus.record(level=level, subsystem="engine:adblock", message=message, context=context, tier="engine")


class AdBlocker:
    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / "adblock"
        self._lock = threading.Lock()
        self._loaded_generation: int | None = None
        self._updating = False

    # ── lists ────────────────────────────────────────────────────────────

    def _list_path(self, name: str) -> Path:
        return self.dir / f"{name}.txt"

    def _builtin(self) -> Path:
        """The app's own ad domains (dom_cleaner) as a filter list."""
        from .dom_cleaner import EASYLIST_AD_DOMAINS
        text = "".join(f"||{domain}^\n" for domain in sorted(EASYLIST_AD_DOMAINS))
        path = self.dir / "builtin.txt"
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return path

    def lists(self) -> list[Path]:
        """Downloaded lists, or the built-in one when none have been fetched."""
        present = [self._list_path(name) for name in FULL_LISTS if self._list_path(name).is_file()]
        return present or [self._builtin()]

    def stale(self) -> bool:
        paths = [self._list_path(name) for name in FULL_LISTS]
        return any(not p.is_file() or time.time() - p.stat().st_mtime > REFRESH_AFTER for p in paths)

    def update(self) -> dict[str, Any]:
        """Fetch every full list (on the active route); keeps the old copy of any that fails."""
        with self._lock:
            if self._updating:
                return {"updating": True}
            self._updating = True
        results: dict[str, Any] = {}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            for name, url in FULL_LISTS.items():
                try:
                    # easylist.to refuses Python's default agent (403); say who we are.
                    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                    with route_http.urlopen(request, timeout=30) as response:
                        data = response.read(_MAX_LIST_BYTES + 1)
                    if len(data) > _MAX_LIST_BYTES or b"\n" not in data:
                        raise ValueError(f"{len(data)} bytes does not look like a filter list")
                    temp = self._list_path(name).with_suffix(".part")
                    temp.write_bytes(data)
                    temp.replace(self._list_path(name))
                    results[name] = {"ok": True, "bytes": len(data)}
                except Exception as exc:
                    results[name] = {"ok": False, "error": str(exc)}
                    _record("WARN", f"[ADBLOCK_LIST_FAILED] {name}: {exc}", list=name, url=url)
            self._loaded_generation = None  # recompile on next use
            _record("INFO", "[ADBLOCK_LISTS_UPDATED]", results=results)
            return results
        finally:
            self._updating = False

    def status(self) -> dict[str, Any]:
        lists = []
        for name in FULL_LISTS:
            path = self._list_path(name)
            lists.append({"name": name, "bytes": path.stat().st_size if path.is_file() else None,
                          "updated_at": path.stat().st_mtime if path.is_file() else None})
        cache = self.dir / "engine.dat"
        return {"lists": lists, "builtin_only": not any(item["bytes"] for item in lists),
                "cache_bytes": cache.stat().st_size if cache.is_file() else None}

    # ── engine ───────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> rust_session.CoreSession:
        session = rust_session.session()
        with self._lock:
            if self._loaded_generation == session.generation and session.generation:
                return session
            result, generation = session.request(
                "adblock_load", {"lists": [str(p) for p in self.lists()], "cache": str(self.dir / "engine.dat")},
                timeout=60.0)
            self._loaded_generation = generation
        _record("INFO", f"[ADBLOCK_LOADED] {'cache' if result.get('from_cache') else 'compiled'} in {result.get('ms')} ms",
                **result)
        return session

    def check(self, requests: list[dict[str, str]]) -> list[bool] | None:
        """Blocked or not, per request; None when the engine cannot answer."""
        if not requests:
            return []
        try:
            result, _ = self._ensure_loaded().request("adblock_check", {"requests": requests}, timeout=20.0)
            return [bool(r.get("blocked")) for r in result["results"]]
        except Exception as exc:
            _record("WARN", f"[ADBLOCK_UNAVAILABLE] falling back to the built-in ad domain list: {exc}")
            return None

    def cosmetic(self, url: str, classes: list[str] | None = None, ids: list[str] | None = None) -> dict[str, Any] | None:
        try:
            result, _ = self._ensure_loaded().request(
                "adblock_cosmetic", {"url": url, "classes": classes or [], "ids": ids or []}, timeout=20.0)
            return result
        except Exception as exc:
            _record("WARN", f"[ADBLOCK_UNAVAILABLE] no cosmetic rules for {url}: {exc}")
            return None

    # ── consumers ────────────────────────────────────────────────────────

    def strip_page(self, cleaner: Any, page_url: str) -> int:
        """Move elements the filter lists block into the cleaner's stripped ads."""
        elements = [e for e in cleaner.elements if e.target_url]
        verdicts = self.check([{"url": e.target_url, "source_url": page_url, "type": "other"} for e in elements])
        cosmetic = self.cosmetic(page_url, sorted({c for e in cleaner.elements for c in e.element_classes}),
                                 sorted({e.element_id for e in cleaner.elements if e.element_id}))
        hidden = set((cosmetic or {}).get("hide_selectors") or [])
        blocked = {id(e) for e, v in zip(elements, verdicts or []) if v}
        stripped = 0
        kept = []
        for element in cleaner.elements:
            by_rule = id(element) in blocked
            by_style = bool(hidden) and (f"#{element.element_id}" in hidden
                                         or any(f".{c}" in hidden for c in element.element_classes))
            if by_rule or by_style:
                element.is_ad = True
                element.filter_reason = "filter_list" if by_rule else "filter_list_cosmetic"
                cleaner.stripped_ads.append(element)
                stripped += 1
            else:
                kept.append(element)
        cleaner.elements = kept
        return stripped

    def blocked_urls(self, urls: list[str]) -> set[str]:
        verdicts = self.check([{"url": u, "source_url": u, "type": "other"} for u in urls]) or []
        return {u for u, v in zip(urls, verdicts) if v}
