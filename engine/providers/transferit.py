from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

from ..errors import NeedsUser
from ..models import ResolvedItem
from .. import route_http


class TransferItProvider:
    id = "transfer.it"

    @classmethod
    def manifest(cls) -> dict:
        return {
            "id": cls.id, "version": "1.0.0", "hosts": ["transfer.it"],
            "capabilities": ["folders", "zip", "password", "refresh"],
        }

    @classmethod
    def match(cls, url: str) -> bool:
        return "transfer.it/t/" in url.lower()

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        secrets = secrets or {}
        handle, password_token, info, decoded, folder_paths = cls._load_tree(url, secrets)
        selected_ids = cls._selected_ids(secrets.get("selected_item_ids"))
        if selected_ids is not None and not selected_ids:
            raise NeedsUser("No Transfer.it files are selected", "file_selection")
        items: list[ResolvedItem] = []
        file_nodes = [
            node for node in decoded
            if node.get("t") == 0 and (selected_ids is None or node["h"] in selected_ids)
        ]
        if selected_ids is not None and not file_nodes:
            raise NeedsUser("No Transfer.it files are selected", "file_selection")
        if not file_nodes:
            return []

        # Batch request download URLs in chunks of 50 to avoid slow sequential roundtrips
        batch_size = 50
        links: list[dict] = []
        for i in range(0, len(file_nodes), batch_size):
            chunk = file_nodes[i:i + batch_size]
            if len(chunk) == 1:
                req_payload: dict | list[dict] = {"a": "g", "n": chunk[0]["h"], "g": 1, "ssl": 1}
            else:
                req_payload = [{"a": "g", "n": node["h"], "g": 1, "ssl": 1} for node in chunk]
            batch_resp = cls._api(req_payload, handle, password_token)
            if isinstance(batch_resp, dict):
                batch_resp = [batch_resp]
            links.extend(batch_resp)

        for node, resp in zip(file_nodes, links):
            if not isinstance(resp, dict) or not resp.get("g"):
                continue
            name = node.get("name") or node["h"]
            relative = "/".join(part for part in (folder_paths.get(node.get("p", ""), ""), name) if part)
            items.append(ResolvedItem(
                provider=cls.id, source_url=url, display_name=name,
                relative_path=PurePosixPath(relative).as_posix(), size=node.get("s"),
                direct_url=resp["g"], postprocess={"type": "mega-ctr", "key_a32": node.get("key_a32", []), "size": node.get("s")},
                metadata={"handle": handle, "node": node["h"], "mode": "file", "title": info[0].get("t") if info else None},
                item_id=str(node["h"]),
            ))
        if selected_ids is not None and not items:
            raise NeedsUser("The selected Transfer.it files are no longer available", "file_selection")
        return items

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        """Return a complete metadata-only tree without requesting signed URLs."""
        handle, _password_token, info, decoded, folder_paths = cls._load_tree(url, secrets or {})
        if not decoded:
            raise NeedsUser("Transfer.it returned no files or folders", "refresh")
        result: list[ResolvedItem] = []
        for node in decoded:
            is_folder = node.get("t") == 1
            node_id = str(node.get("h", ""))
            name = node.get("name") or node_id
            relative = folder_paths.get(node_id, name) if is_folder else "/".join(
                part for part in (folder_paths.get(node.get("p", ""), ""), name) if part)
            result.append(ResolvedItem(
                provider=cls.id, source_url=url, display_name=name,
                relative_path=PurePosixPath(relative).as_posix(), size=None if is_folder else node.get("s"),
                direct_url=None,
                metadata={"handle": handle, "node": node_id, "parent_id": node.get("p") or None,
                          "type": "folder" if is_folder else "file", "mode": "tree",
                          "title": info[0].get("t") if info else None},
                item_id=f"folder:{node_id}" if is_folder else node_id,
            ))
        return result

    @classmethod
    def _load_tree(cls, url: str, secrets: dict[str, str]):
        handle = cls._handle(url)
        password = secrets.get("password")
        password_token = cls._password_token(handle, password) if password else None
        info = cls._api({"a": "xi", "xh": handle})
        nodes = cls._api({"a": "f", "c": 1, "r": 1}, handle, password_token)
        raw_nodes = nodes[0].get("f", []) if nodes and isinstance(nodes[0], dict) else []
        if not raw_nodes:
            raise NeedsUser("Transfer.it returned no downloadable files", "refresh")
        decoded = []
        for node in raw_nodes:
            key = cls._a32(node.get("k", ""))
            decoded.append({**node, "key_a32": key, "name": cls._decrypt_name(node.get("a", ""), key)})
        folder_paths: dict[str, str] = {}
        for node in decoded:
            if node.get("t") == 1 and not node.get("p"):
                folder_paths[node["h"]] = node.get("name") or ""
        pending = True
        while pending:
            pending = False
            for node in decoded:
                if node.get("t") != 1 or node["h"] in folder_paths:
                    continue
                parent = node.get("p")
                if parent in folder_paths:
                    folder_paths[node["h"]] = "/".join(
                        part for part in (folder_paths[parent], node.get("name") or node["h"]) if part)
                    pending = True
        return handle, password_token, info, decoded, folder_paths

    @staticmethod
    def _selected_ids(value) -> set[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return {part for part in value.split(",") if part}
        if isinstance(value, (list, tuple, set)):
            return {str(part) for part in value if str(part)}
        raise ValueError("selected_item_ids must be an array or comma-separated string")

    @staticmethod
    def _handle(url: str) -> str:
        parts = urllib.parse.urlsplit(url).path.rstrip("/").split("/")
        if len(parts) < 3 or parts[-2] != "t":
            raise ValueError("Transfer.it URL must contain /t/<handle>")
        handle = parts[-1]
        if not handle or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in handle):
            raise ValueError("invalid Transfer.it handle")
        return handle

    @staticmethod
    def _password_token(handle: str, password: str) -> str:
        raw = base64.urlsafe_b64decode(handle + "=" * (-len(handle) % 4))
        salt = raw[-6:] * 3
        token = hashlib.pbkdf2_hmac("sha256", password.strip().encode(), salt, 100_000, 32)
        return base64.urlsafe_b64encode(token).decode().rstrip("=")

    @staticmethod
    def _a32(value: str) -> list[int]:
        if not value:
            return []
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return [int.from_bytes(raw[i:i + 4], "big") for i in range(0, len(raw), 4)]

    @staticmethod
    def _decrypt_name(encoded: str, key: list[int]) -> str | None:
        if not encoded or len(key) < 4:
            return None
        try:
            from Crypto.Cipher import AES
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            expanded = key + [0] * max(0, 8 - len(key))
            aes_key = b"".join((expanded[i] ^ expanded[i + 4]).to_bytes(4, "big") for i in range(4))
            plain = AES.new(aes_key, AES.MODE_CBC, b"\0" * 16).decrypt(raw)
            if not plain.startswith(b"MEGA"):
                return None
            body = plain[4:].rstrip(b"\0").rstrip()
            return str(json.loads(body[:body.rfind(b"}") + 1]).get("n", "")) or None
        except Exception:
            return None

    @staticmethod
    def _api(payload: dict | list[dict], handle: str | None = None, password_token: str | None = None) -> list:
        params = {"id": str(secrets.randbelow(1_000_000_000))}
        if handle:
            params["x"] = handle
            if password_token:
                params["pw"] = password_token
        query = urllib.parse.urlencode(params)
        body = payload if isinstance(payload, list) else [payload]
        request = urllib.request.Request(
            f"https://g.api.mega.co.nz/cs?{query}", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "transfer-manager/0.1"},
        )
        try:
            with route_http.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Transfer.it API HTTP error {exc.code}") from exc
        if data and isinstance(data[0], int) and data[0] < 0:
            err_code = data[0]
            if err_code == -14:
                raise NeedsUser("Transfer.it link requires a password", "password")
            if err_code in {-17, -509}:
                raise RuntimeError("Transfer.it bandwidth quota exceeded")
            if err_code == -9:
                raise RuntimeError("Transfer.it item not found, deleted, or link has expired")
            if err_code == -11:
                raise RuntimeError("Transfer.it access denied or link is private")
            if err_code == -16:
                raise RuntimeError("Transfer.it file or folder is blocked")
            if err_code == -2:
                raise RuntimeError("Invalid Transfer.it link format or parameters")
            raise RuntimeError(f"Transfer.it API error {err_code}")
        return data

    @classmethod
    def refresh(cls, item: ResolvedItem, reason: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        return cls.resolve(item.source_url, secrets)
