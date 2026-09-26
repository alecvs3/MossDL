from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath

from ..errors import NeedsUser, ProviderMappedError, ProviderUnavailable
from ..models import ResolvedItem
from .. import route_http


class MegaProvider:
    id = "mega"

    @classmethod
    def manifest(cls) -> dict:
        return {"id": cls.id, "version": "1.0.0", "hosts": ["mega.nz"], "capabilities": ["folders", "encrypted", "accounts"]}

    @classmethod
    def match(cls, url: str) -> bool:
        return "mega.nz/" in url.lower()

    @classmethod
    def _parse_url(cls, url: str) -> tuple[str, str, str]:
        match = re.search(r"/(file|folder)/([^#?]+)", url)
        if not match:
            raise ProviderUnavailable("Unsupported MEGA URL")
        kind, handle = match.groups()
        fragment = urllib.parse.urlsplit(url).fragment
        key = fragment.split("/", 1)[0]
        if not key:
            raise NeedsUser("MEGA URL has no decryption key", "mega_key")
        return kind, handle, key

    @classmethod
    def _api(cls, payload: dict, public_handle: str | None = None) -> list:
        query = {"id": str(secrets.randbelow(1_000_000_000))}
        if public_handle:
            query["n"] = public_handle
        request = urllib.request.Request(
            "https://g.api.mega.co.nz/cs?" + urllib.parse.urlencode(query),
            data=json.dumps([payload]).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "transfer-manager/0.1"},
        )
        try:
            with route_http.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            retry_after = None
            try:
                retry_after = float(exc.headers.get("Retry-After")) if exc.headers.get("Retry-After") else None
            except (TypeError, ValueError):
                retry_after = None
            category = "rate_limited" if exc.code in {429, 509} else "provider_unavailable"
            raise ProviderMappedError(
                f"MEGA API returned HTTP {exc.code}; please retry",
                category, exc.code, retry_after,
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            reason = str(getattr(exc, "reason", exc)).strip()
            raise ProviderMappedError(
                f"MEGA API is unreachable ({reason}); check your connection or firewall and retry",
                "transient_network",
            ) from exc
        if data and isinstance(data[0], int) and data[0] < 0:
            code = int(data[0])
            categories = {-4: ("rate_limited", "MEGA rate limit reached"),
                          -8: ("provider_rejected", "MEGA says this item is no longer available"),
                          -9: ("provider_rejected", "MEGA says this item is no longer available"),
                          -16: ("authentication", "MEGA rejected the request"),
                          -17: ("rate_limited", "MEGA transfer limit reached"),
                          -24: ("rate_limited", "MEGA transfer limit reached"),
                          -401: ("rate_limited", "MEGA transfer limit reached")}
            category, message = categories.get(code, ("provider_rejected", f"MEGA API error {code}"))
            raise ProviderMappedError(message, category, 410 if code in {-8, -9} else None)
        return data

    @staticmethod
    def _a32(value: str | list[int] | None) -> list[int]:
        if isinstance(value, list):
            return [int(part) for part in value]
        if not value:
            return []
        encoded = str(value).split("/")[-1]
        if ":" in encoded:
            encoded = encoded.rsplit(":", 1)[-1]
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        return [int.from_bytes(raw[index:index + 4], "big") for index in range(0, len(raw), 4)]

    @staticmethod
    def _key_bytes(key: list[int]) -> bytes:
        return b"".join(value.to_bytes(4, "big") for value in key[:4])

    @staticmethod
    def _download_request(node_handle: str, public_folder_handle: str | None = None) -> tuple[dict, str | None]:
        """Build the public-download request used by async-mega-py."""
        field = "n" if public_folder_handle else "p"
        return {"a": "g", field: node_handle, "g": 1}, public_folder_handle

    @classmethod
    def _decrypt_node_key(cls, encoded: str | list[int] | None, shared_key: list[int]) -> list[int]:
        """Decrypt a public-folder node key using the folder's shared key."""
        encrypted = cls._a32(encoded)
        if len(shared_key) < 4 or len(encrypted) < 4 or len(encrypted) % 4:
            return []
        try:
            from Crypto.Cipher import AES
            cipher = AES.new(cls._key_bytes(shared_key), AES.MODE_ECB)
            raw = b"".join(value.to_bytes(4, "big") for value in encrypted)
            plain = b"".join(cipher.decrypt(raw[index:index + 16]) for index in range(0, len(raw), 16))
            return [int.from_bytes(plain[index:index + 4], "big") for index in range(0, len(plain), 4)]
        except (ImportError, ValueError, binascii.Error):
            return []

    @staticmethod
    def _attribute_key(node_key: list[int], is_folder: bool) -> list[int]:
        if not is_folder and len(node_key) >= 8:
            return [node_key[index] ^ node_key[index + 4] for index in range(4)]
        return node_key[:4]

    @classmethod
    def _decrypt_attributes(cls, encoded: str | None, key: list[int]) -> dict:
        if not encoded or len(key) < 4:
            return {}
        try:
            from Crypto.Cipher import AES
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            plain = AES.new(cls._key_bytes(key), AES.MODE_CBC, b"\0" * 16).decrypt(raw)
            if not plain.startswith(b"MEGA"):
                return {}
            body = plain[4:].rstrip(b"\0").rstrip()
            start, end = body.find(b"{"), body.rfind(b"}")
            return json.loads(body[start:end + 1]) if start >= 0 and end > start else {}
        except Exception:
            return {}

    @classmethod
    def _node_name(cls, node: dict, public_key: list[int]) -> str:
        plain_name = node.get("name") or node.get("n")
        if isinstance(plain_name, str) and plain_name:
            return plain_name
        node_key = cls._decrypt_node_key(node.get("key_a32") or node.get("k"), public_key) or public_key
        attrs = cls._decrypt_attributes(
            node.get("a") or node.get("at"),
            cls._attribute_key(node_key, node.get("t") == 1),
        )
        return str(attrs.get("n") or node.get("h") or "mega-item")

    @classmethod
    def _folder_nodes(cls, handle: str) -> list[dict]:
        result = cls._api({"a": "f", "c": 1, "r": 1, "ca": 1}, handle)
        first = result[0] if result and isinstance(result[0], dict) else {}
        nodes = first.get("f", [])
        if not nodes:
            raise ProviderUnavailable("MEGA folder contains no files or folders")
        return [node for node in nodes if isinstance(node, dict) and node.get("h")]

    @classmethod
    def _tree_items(cls, url: str, kind: str, handle: str, key: str,
                    nodes: list[dict] | None = None) -> list[ResolvedItem]:
        public_key = cls._a32(key)
        if kind == "folder":
            nodes = nodes if nodes is not None else cls._folder_nodes(handle)
        else:
            # Public files use `p`; `n` is reserved for authenticated/private
            # nodes. Using `n` here can produce MEGA's misleading -9 response.
            payload, context = cls._download_request(handle)
            response = cls._api(payload, context)
            node = response[0] if response and isinstance(response[0], dict) else {"h": handle, "t": 0}
            nodes = [node]
        names = {str(node["h"]): cls._node_name(node, public_key) for node in nodes}
        paths: dict[str, str] = {}
        pending = list(nodes)
        while pending:
            progressed = False
            for node in pending[:]:
                node_id = str(node["h"])
                parent = str(node.get("p") or "")
                if not parent or parent not in names:
                    paths[node_id] = names[node_id]
                    pending.remove(node)
                    progressed = True
                elif parent in paths:
                    paths[node_id] = "/".join(part for part in (paths[parent], names[node_id]) if part)
                    pending.remove(node)
                    progressed = True
            if not progressed:
                for node in pending:
                    paths[str(node["h"])] = names[str(node["h"])]
                break
        items: list[ResolvedItem] = []
        for node in nodes:
            node_id = str(node["h"])
            is_folder = node.get("t") == 1
            parent_id = str(node.get("p")) if node.get("p") else None
            item_name = names[node_id]
            relative = PurePosixPath(paths[node_id]).as_posix()
            items.append(ResolvedItem(
                provider=cls.id, source_url=url, display_name=item_name, relative_path=relative,
                size=None if is_folder else node.get("s"), direct_url=None,
                item_id=f"folder:{node_id}" if is_folder else node_id,
                metadata={"handle": handle, "node": node_id, "parent_id": parent_id,
                          "type": "folder" if is_folder else "file", "mode": "tree",
                          "encrypted": True},
            ))
        return items

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        kind, handle, key = cls._parse_url(url)
        if kind != "folder":
            # A single public file has no useful tree to pick from, but this
            # keeps enumerate compatible for callers that request metadata.
            return cls._tree_items(url, kind, handle, key)
        return cls._tree_items(url, kind, handle, key)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, str] | None = None) -> list[ResolvedItem]:
        # This adapter resolves public metadata and returns an explicit
        # postprocess contract. Decryption is performed by the engine worker,
        # never by the UI or an untrusted plugin.
        secrets = secrets or {}
        kind, handle, key = cls._parse_url(url)
        selected_value = secrets.get("selected_item_ids")
        if selected_value is None:
            selected = None
        elif isinstance(selected_value, str):
            selected = {part.strip() for part in selected_value.split(",") if part.strip()}
        elif isinstance(selected_value, (list, tuple, set)):
            selected = {str(part) for part in selected_value if str(part)}
        else:
            raise ValueError("selected_item_ids must be an array or comma-separated string")
        if selected is not None and not selected:
            raise NeedsUser("No MEGA files are selected", "file_selection")
        folder_nodes = cls._folder_nodes(handle) if kind == "folder" else None
        tree = cls._tree_items(url, kind, handle, key, folder_nodes)
        public_key = cls._a32(key)
        node_keys = {
            str(node["h"]): cls._decrypt_node_key(node.get("k"), public_key)
            for node in (folder_nodes or []) if node.get("h")
        }
        items: list[ResolvedItem] = []
        for metadata_item in tree:
            if metadata_item.metadata.get("type") != "file":
                continue
            if selected is not None and metadata_item.item_id not in selected:
                continue
            node_handle = str(metadata_item.metadata["node"])
            # A child of a public folder is still a public node. The folder
            # handle belongs in the request query as `n`, while the child
            # handle belongs in the payload as `p`.
            # Cyberdrop-DL/async-mega-py uses `n` for a child node when the
            # public-folder handle is supplied as request context. Direct
            # public files use `p`. Sending `p` for a folder child returns
            # MEGA error -9 even though the node is valid.
            payload, context = cls._download_request(node_handle, handle if kind == "folder" else None)
            response = cls._api(payload, context)
            value = response[0] if response and isinstance(response[0], dict) else {}
            direct = value.get("g")
            if not direct:
                raise ProviderUnavailable("MEGA did not return a signed download URL")
            node_key = node_keys.get(node_handle) or cls._a32(value.get("key_a32")) or cls._a32(key)
            items.append(ResolvedItem(
                provider=cls.id, source_url=url, display_name=metadata_item.display_name,
                relative_path=metadata_item.relative_path, size=metadata_item.size, direct_url=direct,
                metadata={**metadata_item.metadata, "key": key},
                postprocess={"type": "mega-ctr", "key_a32": node_key, "size": metadata_item.size},
                item_id=metadata_item.item_id,
            ))
        if not items:
            raise NeedsUser("The selected MEGA files are no longer available", "file_selection")
        return items
