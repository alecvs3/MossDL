"""Bounded, resumable collection planning independent of provider UI."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any, Iterable

from .models import CollectionItem, CollectionPlan, ResolvedItem


def stable_item_id(provider_id: str | None, source_url: str, item_id: str | None = None) -> str:
    value = f"{provider_id or 'generic'}\0{canonical_url(source_url)}\0{item_id or ''}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def canonical_url(value: str) -> str:
    """Canonical identity only; never returns URL credentials or fragments."""
    parsed = urlsplit(str(value or "").strip())
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", query, ""))


def public_url(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    blocked = {"token", "access_token", "authorization", "signature", "sig", "password", "secret", "cookie"}
    query = urlencode([(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                       if key.lower() not in blocked and not any(marker in key.lower() for marker in ("token", "signature", "secret"))])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


_SECRET_KEYS = {"cookie", "cookies", "token", "tokens", "authorization", "headers", "password", "secret", "signature", "signed_url"}


def redact(value: Any, sensitive: dict[str, str] | None = None) -> Any:
    if isinstance(value, str) and sensitive:
        for secret in sensitive.values():
            if secret:
                value = value.replace(str(secret), "[redacted]")
        return value
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if str(k).lower() in _SECRET_KEYS or any(part in str(k).lower() for part in ("token", "cookie", "password", "authorization", "secret", "signature")) else redact(v, sensitive)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, sensitive) for item in value]
    if isinstance(value, tuple):
        return [redact(item, sensitive) for item in value]
    return value


def outcome_for_error(exc: BaseException) -> str:
    text = str(exc).lower()
    category = str(getattr(exc, "category", "")).lower()
    if "captcha" in text or category == "captcha":
        return "captcha"
    if "login" in text or "auth" in text or category in {"login_required", "authentication"}:
        return "login_required"
    if "unsupported" in text or category == "unsupported":
        return "unsupported"
    if "quota" in text or category == "quota":
        return "quota"
    return "partial_failure"


class CollectionPlanner:
    def __init__(self, max_pages: int = 100, max_items: int = 10000, max_depth: int = 8) -> None:
        self.max_pages, self.max_items, self.max_depth = max_pages, max_items, max_depth

    def create(self, plan_id: str, source_url: str, provider_id: str | None,
               items: Iterable[ResolvedItem | dict[str, Any]] = (), *, page: int = 0,
               cursor: str | None = None, has_more: bool = False) -> CollectionPlan:
        result: list[CollectionItem] = []
        seen: set[str] = set()
        for raw in items:
            value = raw.to_dict() if isinstance(raw, ResolvedItem) else dict(raw)
            item_id = stable_item_id(provider_id, value.get("source_url", source_url), value.get("item_id") or value.get("id"))
            if item_id in seen:
                continue
            seen.add(item_id)
            result.append(CollectionItem(item_id, value.get("source_url", source_url),
                                         value.get("display_name") or value.get("name") or "download",
                                         value.get("relative_path") or value.get("path") or "",
                                         value.get("size"), bool(value.get("selected", False)),
                                         value.get("status", "pending"), value.get("error"), redact(value.get("metadata", {})),
                                         value.get("parent_id"), int(value.get("depth", 0)), int(value.get("page", page)), value.get("cursor"),
                                         value.get("folder_path", value.get("relative_path", "")), value.get("package_path", ""), value.get("outcome")))
            if len(result) >= self.max_items:
                has_more = True
                break
        return CollectionPlan(plan_id, source_url, provider_id, result, cursor, page, has_more,
                              self.max_pages, self.max_items, "active", None, 0, self.max_depth)

    def crawl(self, plan_id: str, source_url: str, provider_id: str | None,
              fetch_page: Any, *, max_depth: int | None = None, max_pages: int | None = None,
              max_items: int | None = None, cursor: str | None = None,
              cancel: Any = None, secrets: dict[str, str] | None = None) -> CollectionPlan:
        """Traverse provider pages while the engine owns bounds and lifecycle."""
        depth_limit = self.max_depth if max_depth is None else max(0, int(max_depth))
        page_limit = self.max_pages if max_pages is None else max(1, int(max_pages))
        item_limit = self.max_items if max_items is None else max(1, int(max_items))
        plan = CollectionPlan(plan_id, public_url(source_url), provider_id, [], cursor=cursor, max_pages=page_limit,
                              max_items=item_limit, max_depth=depth_limit, nodes=[], events=[])
        queue: list[tuple[str, str | None, int, str]] = [(source_url, None, 0, "")]
        visited: set[str] = set()
        seen_node_urls: set[str] = set()
        pages = 0
        while queue and pages < page_limit and len(plan.nodes) < item_limit:
            if cancel is not None and cancel():
                plan.state, plan.outcome = "canceled", "canceled"
                break
            url, parent_id, depth, folder = queue.pop(0)
            identity = canonical_url(url)
            if identity in visited:
                plan.events.append({"outcome": "cycle", "source_url": identity, "parent_id": parent_id})
                continue
            visited.add(identity)
            pages += 1
            try:
                raw = fetch_page(url, secrets)
                candidates = raw if isinstance(raw, list) else (raw.get("items", []) if isinstance(raw, dict) else [])
                next_cursor = raw.get("cursor") if isinstance(raw, dict) else None
                has_more = bool(raw.get("has_more")) if isinstance(raw, dict) else False
                for candidate in candidates:
                    value = candidate.to_dict() if isinstance(candidate, ResolvedItem) else dict(candidate)
                    child_url = str(value.get("source_url") or value.get("url") or url)
                    item_id = stable_item_id(provider_id, child_url, value.get("item_id") or value.get("id"))
                    child_identity = canonical_url(child_url)
                    if child_identity in seen_node_urls or any(node["node_id"] == item_id for node in plan.nodes):
                        plan.events.append({"outcome": "duplicate", "node_id": item_id})
                        continue
                    seen_node_urls.add(child_identity)
                    node = {"node_id": item_id, "parent_id": value.get("parent_id", parent_id), "depth": depth,
                            "source_url": public_url(child_url), "canonical_url": canonical_url(public_url(child_url)), "page": pages,
                            "cursor": next_cursor, "folder_path": value.get("folder_path", value.get("relative_path", folder)),
                            "package_path": value.get("package_path", ""), "display_name": value.get("display_name", value.get("name", "download")),
                            "provider": value.get("provider", provider_id), "item_id": value.get("item_id"),
                            "size": value.get("size"), "status": value.get("status", "discovered"), "outcome": value.get("outcome"),
                            "metadata": redact(value.get("metadata", {}), secrets)}
                    plan.nodes.append(redact(node, secrets))
                    plan.items.append(CollectionItem(item_id, public_url(child_url), node["display_name"], node["folder_path"], node["size"],
                                                     False, node["status"], None, node["metadata"], node["parent_id"], depth,
                                                     pages, next_cursor, node["folder_path"], node["package_path"], node["outcome"]))
                    child_urls = value.get("children") or value.get("links") or []
                    if child_urls and depth < depth_limit:
                        for child in child_urls:
                            child_value = child if isinstance(child, str) else child.get("source_url") or child.get("url")
                            if child_value:
                                child_value = str(child_value)
                                child_identity = canonical_url(child_value)
                                if child_identity in visited or child_identity in seen_node_urls:
                                    plan.events.append({"outcome": "cycle", "source_url": public_url(child_value), "parent_id": item_id})
                                    continue
                                child_id = stable_item_id(provider_id, child_value, None)
                                seen_node_urls.add(child_identity)
                                child_node = {"node_id": child_id, "parent_id": item_id, "depth": depth + 1,
                                              "source_url": public_url(child_value), "canonical_url": canonical_url(public_url(child_value)),
                                              "page": pages, "cursor": next_cursor, "folder_path": node["folder_path"],
                                              "package_path": node["package_path"], "display_name": PurePosixPath(urlsplit(child_value).path).name or "download",
                                              "provider": provider_id, "item_id": None, "size": None, "status": "discovered",
                                              "outcome": None, "metadata": {}}
                                plan.nodes.append(child_node)
                                plan.items.append(CollectionItem(child_id, public_url(child_value), child_node["display_name"],
                                                                 child_node["folder_path"], None, False, "discovered", None,
                                                                 {}, item_id, depth + 1, pages, next_cursor, node["folder_path"],
                                                                 node["package_path"], None))
                                queue.append((child_value, item_id, depth + 1, node["folder_path"]))
                    elif child_urls and depth >= depth_limit:
                        node["outcome"] = "quota"
                plan.cursor, plan.has_more = next_cursor, bool(has_more or queue)
                plan.page = pages
                plan.graph_revision += 1
            except Exception as exc:
                outcome = outcome_for_error(exc)
                plan.outcome = outcome if not plan.nodes else "partial_failure"
                plan.events.append({"outcome": outcome, "source_url": public_url(identity), "error": redact(str(exc)[:500], secrets)})
                plan.has_more = bool(queue)
                if not queue:
                    break
        if not queue:
            plan.has_more = False
        if len(plan.nodes) >= item_limit and (queue or plan.has_more):
            plan.outcome, plan.has_more = "quota", True
        elif pages >= page_limit and queue:
            plan.outcome, plan.has_more = "quota", True
        elif plan.state != "canceled":
            plan.state = "complete" if not plan.has_more else "active"
            if plan.outcome is None:
                plan.outcome = "success"
        return plan

    @staticmethod
    def select(plan: CollectionPlan, selected_ids: Iterable[str], selected: bool = True) -> CollectionPlan:
        wanted = set(selected_ids)
        for item in plan.items:
            if item.stable_id in wanted:
                item.selected = selected
        return plan

    def append(self, plan: CollectionPlan, items: Iterable[ResolvedItem | dict[str, Any]], *,
               cursor: str | None = None, has_more: bool = False) -> CollectionPlan:
        existing = {item.stable_id for item in plan.items}
        for raw in items:
            value = raw.to_dict() if isinstance(raw, ResolvedItem) else dict(raw)
            item_id = stable_item_id(plan.provider_id, plan.source_url, value.get("item_id") or value.get("id"))
            if item_id in existing:
                continue
            plan.items.append(CollectionItem(item_id, value.get("source_url", plan.source_url),
                                             value.get("display_name") or value.get("name") or "download",
                                             value.get("relative_path") or value.get("path") or "", value.get("size"),
                                             bool(value.get("selected", False)), value.get("status", "pending"),
                                             value.get("error"), value.get("metadata", {})))
            existing.add(item_id)
            if len(plan.items) >= plan.max_items:
                has_more = True
                break
        plan.page += 1
        plan.cursor, plan.has_more = cursor, has_more
        if plan.page >= plan.max_pages:
            plan.has_more = False
            plan.state = "complete"
        return plan

    @staticmethod
    def update_status(plan: CollectionPlan, stable_id: str, status: str, error: str | None = None) -> CollectionPlan:
        for item in plan.items:
            if item.stable_id == stable_id:
                item.status, item.error = status, error
                break
        else:
            raise KeyError("collection item not found")
        return plan
