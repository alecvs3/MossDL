from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request

from ..errors import NeedsUser, ProviderUnavailable
from ..models import ResolvedItem
from .. import route_http


_TEMP_TOKEN_CACHE: dict[str, Any] = {"token": "", "created_at": 0.0}


def _get_temp_token(user_agent: str) -> str:
    now = time.time()
    if _TEMP_TOKEN_CACHE["token"] and now - _TEMP_TOKEN_CACHE["created_at"] < 86400:
        return _TEMP_TOKEN_CACHE["token"]
    try:
        req = urllib.request.Request("https://api.gofile.io/accounts", data=b"{}", headers={
            "User-Agent": user_agent, "Origin": "https://gofile.io", "Referer": "https://gofile.io/",
            "Content-Type": "application/json"
        }, method="POST")
        with route_http.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "ok":
                token = data.get("data", {}).get("token", "")
                if token:
                    _TEMP_TOKEN_CACHE["token"] = token
                    _TEMP_TOKEN_CACHE["created_at"] = now
                    return token
    except Exception:
        pass
    return ""


def _get_json(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with route_http.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("status") != "ok":
        raise ProviderUnavailable(f"Gofile API failed: {data.get('status', 'unknown')}")
    return data.get("data", {})


class GofileProvider:
    id = "gofile"

    @classmethod
    def manifest(cls) -> dict:
        return {"id": cls.id, "version": "1.0.0", "hosts": ["gofile.io"], "capabilities": ["folders", "password", "token"]}

    @classmethod
    def match(cls, url: str) -> bool:
        return "gofile.io/" in url.lower()

    @classmethod
    def _content_data(cls, url: str, secrets: dict[str, str] | None = None) -> tuple[dict, dict[str, str]]:
        parsed = urllib.parse.urlsplit(url)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ProviderUnavailable("Invalid Gofile URL")
        content_id = parts[-1]
        if len(parts) >= 2 and parts[0] == "download":
            content_id = parts[1] if parts[1] != "web" else parts[2]
        elif len(parts) >= 2 and parts[-2].lower() == "d":
            content_id = parts[-1]

        secrets = secrets or {}
        password = secrets.get("password") or urllib.parse.parse_qs(parsed.query).get("password", [""])[0]
        query = {"cache": "true", "sortField": "createTime", "sortDirection": "1", "pageSize": "100"}
        if password:
            query["password"] = hashlib.sha256(password.encode()).hexdigest()
        user_agent = "transfer-manager/0.1"
        token = secrets.get("token") or _get_temp_token(user_agent)
        slot = int(time.time()) // 14400
        website_token = hashlib.sha256(f"{user_agent}::en-US::{token}::{slot}::12af056dacea0b".encode()).hexdigest()
        headers = {"User-Agent": user_agent, "X-Website-Token": website_token, "X-BL": "en-US",
                   "Referer": "https://gofile.io/", "Origin": "https://gofile.io"}
        if secrets.get("cookie"):
            headers["Cookie"] = secrets["cookie"]
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = _get_json(f"https://api.gofile.io/contents/{content_id}?{urllib.parse.urlencode(query)}", headers)
        if data.get("password") and data.get("passwordStatus") != "passwordOk":
            raise NeedsUser("Gofile link requires a password", "password")
        return data, secrets

    @staticmethod
    def _selected_ids(value) -> set[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return {part.strip() for part in value.split(",") if part.strip()}
        if isinstance(value, (list, tuple, set)):
            return {str(part) for part in value if str(part)}
        raise ValueError("selected_item_ids must be an array or comma-separated string")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        secrets = secrets or {}
        data, _ = cls._content_data(url, secrets)
        items: list[ResolvedItem] = []
        selected = cls._selected_ids(secrets.get("selected_item_ids"))
        if selected is not None and not selected:
            raise NeedsUser("No Gofile files are selected", "file_selection")

        def walk(node: dict, prefix: str = "") -> None:
            if node.get("type") != "folder":
                if selected is not None and str(node.get("id")) not in selected:
                    return
                name = node.get("name") or node.get("id") or "download"
                if not node.get("link"):
                    raise ProviderUnavailable(f"Gofile file {node.get('id', name)} has no download link")
                items.append(ResolvedItem(cls.id, url, name, f"{prefix}/{name}".lstrip("/"), node.get("size"), node.get("link"),
                                          metadata={"content_id": node.get("id"), "content_type": node.get("type")} ,
                                          item_id=str(node.get("id") or name), expires_at=node.get("expireTime")))
                return
            next_prefix = f"{prefix}/{node.get('name', '')}".strip("/") if node.get("name") else prefix
            for child in (node.get("children") or {}).values():
                walk(child, next_prefix)

        walk(data)
        if not items:
            raise ProviderUnavailable("Gofile content contains no downloadable files")
        return items

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        """Return the complete content tree without resolving file links."""
        data, _ = cls._content_data(url, secrets or {})
        items: list[ResolvedItem] = []

        def walk(node: dict, prefix: str = "", parent_id: str | None = None) -> None:
            node_id = str(node.get("id") or node.get("name") or f"node-{len(items)}")
            is_folder = node.get("type") == "folder"
            name = str(node.get("name") or node_id)
            relative = f"{prefix}/{name}".strip("/")
            items.append(ResolvedItem(
                provider=cls.id, source_url=url, display_name=name, relative_path=relative,
                size=None if is_folder else node.get("size"), direct_url=None,
                expires_at=node.get("expireTime"), item_id=f"folder:{node_id}" if is_folder else node_id,
                metadata={"content_id": node_id, "node": node_id, "parent_id": parent_id,
                          "content_type": node.get("type"), "type": "folder" if is_folder else "file",
                          "mode": "tree"},
            ))
            if is_folder:
                for child in (node.get("children") or {}).values():
                    walk(child, relative, node_id)

        walk(data)
        if not items:
            raise ProviderUnavailable("Gofile content contains no files or folders")
        return items
