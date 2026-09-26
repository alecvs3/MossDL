"""Constrained JSONC provider recipes.

Recipes intentionally expose data extraction and HTTP request composition only.
They are not a general-purpose scripting language and cannot execute code or
access the local filesystem.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from . import route_http
from .errors import NeedsUser, ProviderMappedError, ProviderUnavailable
from .models import ResolvedItem
from .plugin_catalog import loads_jsonc


_CATEGORIES = {
    "expired_url", "password_required", "quota_exceeded", "authentication_required",
    "not_found", "retryable", "policy_denied", "needs_user",
}
_FORBIDDEN_KEYS = {"python", "javascript", "script", "exec", "command", "shell", "eval", "code"}
_METHODS = {"GET", "HEAD", "POST"}


class RecipeError(ProviderUnavailable):
    """A malformed or unsafe recipe."""


def _walk_forbidden(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise RecipeError(f"recipe field {key!r} is not permitted")
            _walk_forbidden(child)
    elif isinstance(value, list):
        for child in value:
            _walk_forbidden(child)


def validate_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(recipe, dict):
        raise RecipeError("recipe root must be an object")
    _walk_forbidden(recipe)
    operations = recipe.get("operations")
    if not isinstance(operations, dict) or not operations:
        raise RecipeError("recipe requires an operations object")
    for name, operation in operations.items():
        if name not in {"resolve", "metadata", "enumerate", "refresh", "extract_links", "health", "authenticate"}:
            raise RecipeError(f"unsupported recipe operation: {name}")
        if not isinstance(operation, dict):
            raise RecipeError(f"recipe operation {name} must be an object")
        request = operation.get("request")
        if request is not None:
            if not isinstance(request, dict):
                raise RecipeError(f"recipe operation {name}.request must be an object")
            method = str(request.get("method", "GET")).upper()
            if method not in _METHODS:
                raise RecipeError(f"unsupported recipe request method: {method}")
        extract = operation.get("extract", operation.get("response", {}))
        if extract is not None and not isinstance(extract, dict):
            raise RecipeError(f"recipe operation {name}.extract must be an object")
    error_mappings = recipe.get("errors", [])
    if not isinstance(error_mappings, list):
        raise RecipeError("recipe errors must be an array")
    for mapping in error_mappings:
        if not isinstance(mapping, dict):
            raise RecipeError("recipe errors must be objects")
        if mapping.get("category") not in _CATEGORIES:
            raise RecipeError("recipe error mapping has an unsupported category")
        if "status" in mapping and not isinstance(mapping["status"], list):
            raise RecipeError("recipe error status must be an array")
    limits = recipe.get("limits", {})
    if not isinstance(limits, dict):
        raise RecipeError("recipe limits must be an object")
    for key in ("response_bytes", "redirects", "pages", "items"):
        if key in limits and (not isinstance(limits[key], int) or limits[key] <= 0):
            raise RecipeError(f"recipe limit {key} must be a positive integer")
    return recipe


def load_recipe(plugin_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    recipe_name = manifest.get("recipe")
    if not isinstance(recipe_name, str):
        raise RecipeError("recipe manifest has no recipe file")
    root = plugin_dir.resolve()
    path = (root / recipe_name).resolve()
    if root not in path.parents and path != root:
        raise RecipeError("recipe file must remain inside the plugin directory")
    if not path.is_file():
        raise RecipeError(f"recipe file not found: {recipe_name}")
    return validate_recipe(loads_jsonc(path.read_text(encoding="utf-8")))


def _json_path(value: Any, expression: str) -> list[Any]:
    if not isinstance(expression, str) or not expression.startswith("$"):
        return []
    current = [value]
    if expression == "$":
        return current
    tokens = re.findall(r"\.([A-Za-z0-9_-]+)|\[([^\]]+)\]", expression[1:])
    consumed = "".join((f".{field}" if field else f"[{index}]") for field, index in tokens)
    if consumed != expression[1:]:
        return []
    for field, index in tokens:
        next_values: list[Any] = []
        if field:
            for item in current:
                if isinstance(item, dict) and field in item:
                    next_values.append(item[field])
        elif index == "*":
            for item in current:
                if isinstance(item, list):
                    next_values.extend(item)
                elif isinstance(item, dict):
                    next_values.extend(item.values())
        elif index.isdigit():
            offset = int(index)
            for item in current:
                if isinstance(item, list) and offset < len(item):
                    next_values.append(item[offset])
        current = next_values
    return current


class _HtmlNodeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = {"tag": tag.lower(), "attrs": {key.lower(): value or "" for key, value in attrs}, "text": ""}
        self.nodes.append(node)
        self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1]["text"] += data

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]["tag"] == tag:
                del self._stack[index:]
                return


def _css_match(node: dict[str, Any], selector: str) -> bool:
    selector = selector.strip().split()[-1]
    attr_match = re.search(r"\[([\w:-]+)(?:=[\"']?([^\]\"']+)[\"']?)?\]", selector)
    if attr_match:
        key, expected = attr_match.groups()
        actual = node["attrs"].get(key.lower())
        if actual is None or expected is not None and actual != expected:
            return False
        selector = selector[:attr_match.start()] + selector[attr_match.end():]
    id_match = re.search(r"#([\w-]+)", selector)
    if id_match and node["attrs"].get("id") != id_match.group(1):
        return False
    class_names = re.findall(r"\.([\w-]+)", selector)
    actual_classes = set(node["attrs"].get("class", "").split())
    if any(name not in actual_classes for name in class_names):
        return False
    tag = re.match(r"^[A-Za-z][\w-]*", selector)
    return not tag or node["tag"] == tag.group(0).lower()


def _html_select(body: str, selector: str) -> list[Any]:
    parser = _HtmlNodeParser()
    parser.feed(body)
    match = re.search(r"::(attr|text)(?:\(([^)]*)\))?$", selector)
    if match:
        pseudo, argument = match.groups()
        selector = selector[:match.start()]
    result = [node for node in parser.nodes if _css_match(node, selector)]
    if pseudo == "attr":
        return [node["attrs"].get((argument or "").lower(), "") for node in result]
    if pseudo == "text":
        return [node["text"].strip() for node in result]
    return result


def _first(values: list[Any], default: Any = None) -> Any:
    return values[0] if values else default


def _safe_basename(url: str) -> str:
    value = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1])
    return value or "download"


def _content_disposition_filename(value: str | None) -> str | None:
    """Extract a safe filename from Content-Disposition, including filename*."""
    if not value:
        return None
    extended = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", value, re.I)
    plain = re.search(r"filename\s*=\s*\"([^\"]+)\"|filename\s*=\s*([^;]+)", value, re.I)
    candidate = extended.group(1) if extended else (plain.group(1) or plain.group(2) if plain else None)
    if not candidate:
        return None
    candidate = urllib.parse.unquote(candidate).strip().strip('"').replace("\\", "/")
    candidate = candidate.rsplit("/", 1)[-1]
    return candidate or None


@dataclass
class _Response:
    url: str
    status: int
    headers: Message
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode(self.headers.get_content_charset() or "utf-8", errors="replace")

    @property
    def data(self) -> Any:
        content_type = self.headers.get("Content-Type", "").lower()
        if "json" in content_type or self.body.lstrip().startswith((b"{", b"[")):
            try:
                return json.loads(self.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
        return self.text


class RecipeRuntime:
    def __init__(self, plugin_dir: Path, manifest: dict[str, Any],
                 transport: Callable[[urllib.request.Request, int, int], _Response] | None = None) -> None:
        self.plugin_dir = plugin_dir.resolve()
        self.manifest = manifest
        self.recipe = load_recipe(self.plugin_dir, manifest)
        self.transport = transport
        self.limits = {"response_bytes": 4 * 1024 * 1024, "redirects": 8, "pages": 32, "items": 10_000,
                       **self.recipe.get("limits", {})}

    def call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "health":
            return {"healthy": True, "provider_id": self.manifest["id"]}
        if method not in self.recipe.get("operations", {}):
            if method == "authenticate":
                raise NeedsUser("Recipe provider authentication requires user action", "login")
            raise RecipeError(f"recipe does not implement {method}")
        operation = self.recipe["operations"][method]
        if method == "refresh" and "request" not in operation:
            operation = self.recipe["operations"].get("resolve", operation)
        responses = self._request_pages(operation.get("request", {}), params)
        for response, _context in responses:
            self._raise_mapped_error(response)
        if method == "health":
            return {"healthy": True}
        result: list[dict[str, Any]] = []
        for response, context in responses:
            result.extend(self._extract(operation, response, context, method, require_result=False))
            if len(result) > int(self.limits["items"]):
                raise RecipeError("recipe item count exceeds the configured limit")
        if not result:
            raise ProviderUnavailable("recipe produced no downloadable items")
        return result

    def _context(self, params: dict[str, Any]) -> dict[str, Any]:
        url = str(params.get("url") or params.get("item", {}).get("source_url") or "")
        parsed = urllib.parse.urlsplit(url)
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        secrets = params.get("secrets") or {}
        allowed_secrets = set(self.manifest.get("permissions", {}).get("secrets", []))
        if isinstance(secrets, dict) and allowed_secrets:
            secrets = {key: value for key, value in secrets.items() if key in allowed_secrets or key == "selected_item_ids"}
        return {"url": {"value": url, "scheme": parsed.scheme, "host": parsed.hostname or "",
                         "path": [part for part in parsed.path.split("/") if part],
                         "query": query, "fragment": parsed.fragment},
                "input_url": url, "item": params.get("item") or {}, "secret": secrets,
                "secrets": secrets}

    @staticmethod
    def _lookup(context: dict[str, Any], expression: str) -> Any:
        value: Any = context
        for part in expression.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit():
                value = value[int(part)] if int(part) < len(value) else None
            else:
                return None
        return value

    def _render(self, value: Any, context: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$secret"}:
                name = str(value["$secret"])
                if name not in self.manifest.get("permissions", {}).get("secrets", []):
                    raise RecipeError(f"secret {name!r} is not permitted by the manifest")
                return self._lookup(context, f"secret.{name}") or ""
            return {key: self._render(child, context) for key, child in value.items()}
        if isinstance(value, list):
            return [self._render(child, context) for child in value]
        if not isinstance(value, str):
            return value
        pattern = re.compile(r"\$\{([^}]+)\}|\{([A-Za-z0-9_.-]+)\}")
        matches = list(pattern.finditer(value))
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            result = self._lookup(context, matches[0].group(1) or matches[0].group(2))
            return "" if result is None else result
        return pattern.sub(lambda match: str(self._lookup(context, match.group(1) or match.group(2)) or ""), value)

    def _allowed_host(self, url: str) -> bool:
        hostname = (urllib.parse.urlsplit(url).hostname or "").lower()
        patterns = list(self.manifest.get("hosts", [])) + list(self.manifest.get("permissions", {}).get("hosts", []))
        return bool(hostname) and any(fnmatch.fnmatch(hostname, pattern.lower()) for pattern in patterns)

    def _request(self, spec: dict[str, Any], params: dict[str, Any]) -> tuple[_Response, dict[str, Any]]:
        context = self._context(params)
        url = str(self._render(spec.get("url", "{input_url}"), context))
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not self._allowed_host(url):
            raise RecipeError("recipe request targets a host or scheme outside its permissions")
        query = self._render(spec.get("query", {}), context)
        if query:
            encoded = urllib.parse.urlencode(query, doseq=True)
            url = url + ("&" if "?" in url else "?") + encoded
        headers = {str(key): str(value) for key, value in self._render(spec.get("headers", {}), context).items()}
        cookies = self._render(spec.get("cookies", {}), context)
        if cookies:
            headers.setdefault("Cookie", "; ".join(f"{key}={value}" for key, value in cookies.items()))
        headers.setdefault("User-Agent", "transfer-manager-recipe/1")
        body = self._render(spec.get("body"), context)
        encoded_body = None
        if body is not None:
            if isinstance(body, (dict, list)):
                encoded_body = json.dumps(body).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            else:
                encoded_body = str(body).encode("utf-8")
        request = urllib.request.Request(url, data=encoded_body, method=str(spec.get("method", "GET")).upper(), headers=headers)
        timeout = max(1, int(spec.get("timeout", 30)))
        if self.transport:
            return self.transport(request, timeout, int(self.limits["redirects"])), context
        opener = route_http.opener(_RedirectLimit(int(self.limits["redirects"])))
        try:
            with opener.open(request, timeout=timeout) as response:
                body_bytes = response.read(int(self.limits["response_bytes"]) + 1)
                if len(body_bytes) > int(self.limits["response_bytes"]):
                    raise RecipeError("recipe response exceeds the configured byte limit")
                return _Response(response.geturl(), getattr(response, "status", 200), response.headers, body_bytes), context
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read(int(self.limits["response_bytes"]) + 1)
            return _Response(exc.geturl(), exc.code, exc.headers, body_bytes), context
        except urllib.error.URLError as exc:
            raise ProviderUnavailable(f"recipe request failed: {exc.reason}") from exc

    def _request_pages(self, spec: dict[str, Any], params: dict[str, Any]) -> list[tuple[_Response, dict[str, Any]]]:
        """Fetch a bounded page/cursor sequence declared by the recipe."""
        if not isinstance(spec, dict):
            return [self._request({}, params)]
        pagination = spec.get("pagination")
        if not isinstance(pagination, dict):
            return [self._request(spec, params)]
        request_spec = {key: value for key, value in spec.items() if key != "pagination"}
        max_pages = min(int(self.limits["pages"]), max(1, int(pagination.get("max_pages", self.limits["pages"]))))
        results: list[tuple[_Response, dict[str, Any]]] = []
        seen: set[str] = set()
        next_url: str | None = None
        page = int(pagination.get("start_page", 1))
        cursor: Any = None
        for _ in range(max_pages):
            current = dict(request_spec)
            if next_url:
                current["url"] = next_url
            query = dict(current.get("query") or {})
            if pagination.get("page_param"):
                query[str(pagination["page_param"])] = page
            if pagination.get("cursor_param") and cursor is not None:
                query[str(pagination["cursor_param"])] = cursor
            if query:
                current["query"] = query
            response, context = self._request(current, params)
            if response.url in seen:
                raise RecipeError("recipe pagination loop detected")
            seen.add(response.url)
            results.append((response, context))
            source = response.data
            next_value = self._value(pagination.get("next"), source, response, context) if pagination.get("next") is not None else None
            if isinstance(next_value, list):
                next_value = _first(next_value)
            if next_value in (None, "", False):
                item_selector = pagination.get("items")
                if pagination.get("page_param") and isinstance(item_selector, str) and item_selector.startswith("$") \
                        and _json_path(source, item_selector):
                    page += 1
                    continue
                break
            if pagination.get("cursor_param"):
                cursor = next_value
                next_url = None
            else:
                next_url = urllib.parse.urljoin(response.url, str(next_value))
            if next_url and not self._allowed_host(next_url):
                raise RecipeError("recipe pagination targets a host outside its permissions")
            page += 1
        else:
            raise RecipeError("recipe pagination exceeded the configured page limit")
        return results

    def _raise_mapped_error(self, response: _Response) -> None:
        if response.status < 400:
            return
        data = response.data
        message = str(data)[:500] if data is not None else f"HTTP {response.status}"
        def mapping_priority(mapping: dict[str, Any]) -> int:
            # Specific structured matches must win over broad status/message
            # mappings regardless of their order in JSONC.
            if mapping.get("code") is not None:
                return 4
            if mapping.get("json") is not None and mapping.get("equals"):
                return 3
            if mapping.get("status"):
                return 2
            if mapping.get("message_regex"):
                return 1
            return 0

        mappings = sorted(self.recipe.get("errors", []), key=mapping_priority, reverse=True)
        for mapping in mappings:
            statuses = mapping.get("status", [])
            if statuses and response.status not in statuses:
                continue
            selected = mapping.get("code")
            if selected is None:
                selected = _first(_json_path(data, mapping["json"])) if mapping.get("json") else None
            equals = mapping.get("equals")
            if equals and selected not in equals:
                continue
            pattern = mapping.get("message_regex")
            if pattern and not re.search(str(pattern), message, re.I):
                continue
            category = mapping["category"]
            retry_after = None
            if mapping.get("retry_after"):
                try:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                except ValueError:
                    retry_after = None
            if category == "password_required":
                raise NeedsUser("Provider requires a password", "password", {"category": category})
            if category == "authentication_required":
                raise NeedsUser("Provider authentication is required", "login", {"category": category})
            raise ProviderMappedError(message, category, response.status, retry_after)
        raise ProviderUnavailable(message)

    def _value(self, definition: Any, source: Any, response: _Response, context: dict[str, Any]) -> Any:
        if isinstance(definition, dict):
            if "default" in definition:
                fallback = definition["default"]
            else:
                fallback = None
            if "json" in definition:
                value = _first(_json_path(source, str(definition["json"])), fallback)
            elif "css" in definition:
                value = _first(_html_select(response.text, str(definition["css"])), fallback)
            elif "header" in definition:
                value = response.headers.get(str(definition["header"]), fallback)
            elif "literal" in definition:
                value = definition["literal"]
            else:
                value = fallback
            if "regex" in definition and value is not None:
                match = re.search(str(definition["regex"]), str(value))
                value = match.group(1) if match and match.groups() else (match.group(0) if match else fallback)
            return self._render(value, context)
        if isinstance(definition, str):
            if definition.startswith("$"):
                return _first(_json_path(source, definition))
            if definition.startswith("css:"):
                return _first(_html_select(response.text, definition[4:]))
            if definition.startswith("header:"):
                return response.headers.get(definition[7:])
            return self._render(definition, context)
        return definition

    def _extract(self, operation: dict[str, Any], response: _Response, context: dict[str, Any], method: str,
                 require_result: bool = True) -> Any:
        source = response.data
        extract = operation.get("extract", operation.get("response", {})) or {}
        items_selector = extract.get("items") or extract.get("each")
        entries = _json_path(source, items_selector) if isinstance(items_selector, str) and items_selector.startswith("$") else [source]
        if len(entries) > int(self.limits["items"]):
            raise RecipeError("recipe item count exceeds the configured limit")
        fields = extract.get("fields", extract)
        selected_value = (context.get("secrets") or {}).get("selected_item_ids")
        selected = None if selected_value is None else ({str(value) for value in selected_value} if isinstance(selected_value, list) else {part.strip() for part in str(selected_value).split(",") if part.strip()})
        if selected is not None and not selected:
            raise NeedsUser("No files are selected", "file_selection")
        result: list[dict[str, Any]] = []
        for entry in entries:
            item_context = {**context, "item": entry}
            name = self._value(fields.get("name", "{url.path.0}"), entry, response, item_context)
            item_id = self._value(fields.get("item_id", fields.get("id", "")), entry, response, item_context)
            item_id = str(item_id or name or hashlib.sha256(str(entry).encode()).hexdigest()[:16])
            item_type = str(self._value(fields.get("type", "file"), entry, response, item_context) or "file").lower()
            relative = self._value(fields.get("relative_path", fields.get("path", "")), entry, response, item_context)
            relative = str(relative or name or _safe_basename(response.url)).replace("\\", "/").lstrip("/")
            path_parts = [part for part in relative.split("/") if part]
            if not path_parts or any(part in {".", ".."} for part in path_parts):
                raise RecipeError("recipe produced an unsafe relative path")
            if selected is not None and item_type != "folder" and item_id not in selected:
                continue
            direct = self._value(fields.get("url", fields.get("direct_url", "")), entry, response, item_context)
            if method == "resolve" and not direct:
                direct = response.url
            if method == "enumerate" or item_type == "folder":
                direct = None
            filename = str(name or "").strip()
            if not filename or filename == "download":
                filename = _content_disposition_filename(response.headers.get("Content-Disposition")) or ""
            filename = filename or _safe_basename(response.url)
            value = ResolvedItem(
                provider=self.manifest["id"], source_url=context["input_url"], display_name=filename,
                relative_path=relative, size=self._int_or_none(self._value(fields.get("size"), entry, response, item_context)),
                direct_url=str(direct) if direct else None,
                checksum=self._value(fields.get("checksum"), entry, response, item_context), item_id=item_id,
                metadata={"type": item_type, "node": item_id, "parent_id": self._value(fields.get("parent_id", ""), entry, response, item_context), "recipe": True},
            )
            result.append(value.to_dict())
        if not result and require_result:
            raise ProviderUnavailable("recipe produced no downloadable items")
        if method == "refresh":
            return result
        return result

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class _RedirectLimit(urllib.request.HTTPRedirectHandler):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > self.limit:
            raise RecipeError("recipe redirect limit exceeded")
        return super().redirect_request(req, fp, code, msg, headers, newurl)
