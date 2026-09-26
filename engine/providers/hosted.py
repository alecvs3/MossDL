from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, ClassVar

from ..errors import NeedsUser, ProviderMappedError, ProviderUnavailable
from ..models import ResolvedItem


MAX_RESPONSE = 16 * 1024 * 1024
USER_AGENT = "transfer-manager/0.1"


def _headers(secrets: dict[str, Any] | None = None, accept_json: bool = False) -> dict[str, str]:
    secrets = secrets or {}
    accept_val = "application/json,text/plain,*/*" if accept_json else "text/html,application/json,*/*"
    ua = str(secrets.get("user_agent") or USER_AGENT)
    value = {"User-Agent": ua, "Accept": accept_val}
    if secrets.get("referer"):
        value["Referer"] = str(secrets["referer"])
    cookies_list: list[str] = []
    if secrets.get("cookie"):
        cookies_list.append(str(secrets["cookie"]).strip("; "))
    if secrets.get("cf_clearance") and not any("cf_clearance=" in c for c in cookies_list):
        cookies_list.append(f"cf_clearance={secrets['cf_clearance']}")
    if isinstance(secrets.get("cookies"), dict):
        for ck_name, ck_val in secrets["cookies"].items():
            if not any(f"{ck_name}=" in c for c in cookies_list):
                cookies_list.append(f"{ck_name}={ck_val}")
    if cookies_list:
        value["Cookie"] = "; ".join(cookies_list)
    if secrets.get("api_token"):
        value["Authorization"] = f"Bearer {secrets['api_token']}"
    return value


def _fetch(url: str, secrets: dict[str, Any] | None = None, accept_json: bool = False) -> tuple[bytes, str, Any]:
    from .. import http_client
    headers = _headers(secrets, accept_json=accept_json)
    timeout = float((secrets or {}).get("timeout", 20))
    try:
        response = http_client.get(url, headers=headers, timeout=timeout)
        if response.status in {401, 403}:
            raise ProviderMappedError("provider authentication or access was rejected", "authentication_required", response.status)
        if response.status == 404:
            raise ProviderMappedError("provider item was not found", "not_found", response.status)
        if response.status in {408, 425, 429, 500, 502, 503, 504}:
            retry_after = response.headers.get("Retry-After") if response.headers else None
            raise ProviderMappedError("provider request is retryable", "retryable", response.status,
                                      float(retry_after) if retry_after and retry_after.isdigit() else None)
        if response.status >= 400:
            raise ProviderUnavailable(f"provider request failed with HTTP {response.status}")
        body = response.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise ProviderUnavailable("provider response exceeded the safety limit")
        return body, response.geturl() or url, response.headers
    except (ProviderMappedError, ProviderUnavailable):
        raise
    except Exception as exc:
        raise ProviderUnavailable(f"provider request failed: {exc}") from exc


def _json(url: str, secrets: dict[str, Any] | None = None) -> dict[str, Any]:
    body, _, _ = _fetch(url, secrets, True)
    try:
        value = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProviderUnavailable("provider returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise ProviderUnavailable("provider returned an invalid JSON object")
    return value


def _selected(secrets: dict[str, Any] | None) -> set[str] | None:
    value = (secrets or {}).get("selected_item_ids")
    if value is None:
        return None
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(part) for part in value}
    raise ValueError("selected_item_ids must be an array or comma-separated string")


def _safe_name(value: str, fallback: str = "download") -> str:
    value = os.path.basename(urllib.parse.unquote(html.unescape(value))).strip()
    return value or fallback


def _links(body: bytes, base: str, allowed: tuple[str, ...] = ()) -> list[str]:
    text = body.decode("utf-8", errors="replace")
    result: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"(?:href|src)\s*=\s*[\"']([^\"']+)", text, re.I):
        candidate = urllib.parse.urljoin(base, html.unescape(raw))
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.scheme not in {"http", "https"}:
            continue
        if allowed and not any((parsed.hostname or "").lower().endswith(host) for host in allowed):
            continue
        if candidate not in seen:
            seen.add(candidate); result.append(candidate)
    return result


class HostedProvider:
    id: ClassVar[str] = "hosted"
    hosts: ClassVar[tuple[str, ...]] = ()
    folder_paths: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def match(cls, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme in {"http", "https"} and any((parsed.hostname or "").lower() == host or (parsed.hostname or "").lower().endswith("." + host) for host in cls.hosts)

    @classmethod
    def _item(cls, source: str, item_id: str, name: str, direct: str | None, size: int | None = None,
              relative: str | None = None, metadata: dict[str, Any] | None = None) -> ResolvedItem:
        return ResolvedItem(cls.id, source, _safe_name(name, item_id), relative or _safe_name(name, item_id), size,
                            direct, metadata=metadata or {}, item_id=item_id)


class MediaFireProvider(HostedProvider):
    id, hosts = "mediafire", ("mediafire.com", "www.mediafire.com")

    @staticmethod
    def _parse_keys(url: str) -> tuple[str | None, str | None]:
        parsed = urllib.parse.urlsplit(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        folder_key = None
        file_key = None
        if len(path_parts) >= 2 and path_parts[0] == "folder":
            folder_key = path_parts[1]
        elif len(path_parts) >= 2 and path_parts[0] in {"file", "file_premium"}:
            file_key = path_parts[1]
        elif parsed.query and not ("=" in parsed.query or "&" in parsed.query):
            file_key = parsed.query
        return folder_key, file_key

    @classmethod
    def _extract_direct_from_html(cls, body: bytes, final_url: str) -> str | None:
        text = body.decode("utf-8", "replace")
        # Check scrambled data url
        scrambled_match = re.search(r'data-scrambled-url\s*=\s*["\']([^"\']+)["\']', text, re.I)
        if scrambled_match:
            try:
                import base64
                decoded = base64.b64decode(scrambled_match.group(1)).decode("utf-8", "replace").strip()
                if decoded.startswith("http"):
                    return decoded
            except Exception:
                pass
        # Check download button href
        btn_match = re.search(r'<a[^>]+id\s*=\s*["\']downloadButton["\'][^>]+href\s*=\s*["\']([^"\']+)["\']', text, re.I)
        if not btn_match:
            btn_match = re.search(r'<a[^>]+href\s*=\s*["\']([^"\']+)["\'][^>]+id\s*=\s*["\']downloadButton["\']', text, re.I)
        if btn_match:
            direct = btn_match.group(1).strip()
            if not direct.startswith("javascript:") and not direct.startswith("#"):
                return urllib.parse.urljoin(final_url, html.unescape(direct))

        # Check regex download link pattern
        match = re.search(r"(?:download_link|downloadlink|download)\s*[:=]\s*[\"']([^\"']+)[\"']", text, re.I)
        if match:
            candidate = urllib.parse.urljoin(final_url, html.unescape(match.group(1)))
            if "mediafireusercontent" in candidate or "/download" in candidate:
                return candidate

        for candidate in _links(body, final_url, ("mediafire.com", "mediafireusercontent.com")):
            if "mediafireusercontent" in candidate.lower() or "/download" in candidate.lower():
                return candidate
        return None

    @classmethod
    def _page(cls, url: str, secrets: dict[str, Any] | None = None):
        body, final, headers = _fetch(url, secrets)
        direct = cls._extract_direct_from_html(body, final)
        return body, final, headers, direct

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        folder_key, _ = cls._parse_keys(url)
        if folder_key:
            return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]

        body, final, headers, direct = cls._page(url, secrets)
        if not direct:
            raise ProviderUnavailable("MediaFire download link was not present in the page")

        # Prioritize filename from direct download URL, Content-Disposition header, or page HTML
        direct_name = urllib.parse.unquote(urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]).replace("+", " ").strip()
        header_name = headers.get_filename()
        name = header_name or direct_name or urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1] or "download"
        length = headers.get("Content-Length")
        return [cls._item(url, urllib.parse.urlsplit(final).path, name, direct, int(length) if length and length.isdigit() else None,
                          metadata={"type": "file", "final_url": final, "content_type": headers.get("Content-Type", "")})]

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        folder_key, _ = cls._parse_keys(url)
        if not folder_key:
            return cls.resolve(url, secrets)

        items: list[ResolvedItem] = []
        visited_folders: set[str] = set()

        def fetch_folder(fkey: str, path_prefix: str = "") -> None:
            if fkey in visited_folders or len(visited_folders) > 50:
                return
            visited_folders.add(fkey)
            chunk = 1
            while True:
                api_url = (f"https://www.mediafire.com/api/1.4/folder/get_content.php?"
                           f"folder_key={urllib.parse.quote(fkey)}&content_type=files&chunk={chunk}&chunk_size=1000&filter=public&response_format=json")
                try:
                    data = _json(api_url, secrets)
                except Exception:
                    break
                resp = data.get("response", {})
                content = resp.get("folder_content", {})
                files = content.get("files", [])
                for f in files:
                    quickkey = f.get("quickkey")
                    fname = f.get("filename") or quickkey or "file"
                    rel = f"{path_prefix}/{fname}".strip("/")
                    size = int(f["size"]) if str(f.get("size", "")).isdigit() else None
                    direct = f"https://www.mediafire.com/file/{quickkey}"
                    items.append(cls._item(url, quickkey, fname, direct, size, relative=rel,
                                           metadata={"type": "file", "mode": "tree", "hash": f.get("hash")}))
                if content.get("more_chunks") != "yes" or not files:
                    break
                chunk += 1

            # Check subfolders
            subchunk = 1
            while True:
                api_url = (f"https://www.mediafire.com/api/1.4/folder/get_content.php?"
                           f"folder_key={urllib.parse.quote(fkey)}&content_type=folders&chunk={subchunk}&chunk_size=1000&filter=public&response_format=json")
                try:
                    data = _json(api_url, secrets)
                except Exception:
                    break
                resp = data.get("response", {})
                content = resp.get("folder_content", {})
                subfolders = content.get("folders", [])
                for sf in subfolders:
                    sfkey = sf.get("folderkey")
                    sfname = sf.get("name") or sfkey
                    sub_rel = f"{path_prefix}/{sfname}".strip("/")
                    items.append(cls._item(url, f"folder:{sfkey}", sfname, None, None, relative=sub_rel,
                                           metadata={"type": "folder", "mode": "tree", "folder_key": sfkey}))
                    fetch_folder(sfkey, sub_rel)
                if content.get("more_chunks") != "yes" or not subfolders:
                    break
                subchunk += 1

        try:
            fetch_folder(folder_key)
        except Exception:
            pass

        if items:
            return items

        # Fallback to page scraping if API did not return items
        body, final, _, _ = cls._page(url, secrets)
        for index, candidate in enumerate(_links(body, final, ("mediafire.com", "mediafireusercontent.com"))):
            if "/file/" not in candidate and "download" not in candidate.lower():
                continue
            name = urllib.parse.urlsplit(candidate).path.rsplit("/", 1)[-1] or f"file-{index}"
            items.append(cls._item(url, f"mediafire:{index}:{candidate}", name, candidate, metadata={"type": "file", "mode": "tree"}))
        if not items:
            return cls.resolve(url, secrets)
        return items



class GoogleDriveProvider(HostedProvider):
    id, hosts = "google-drive", ("drive.google.com", "docs.google.com")

    @staticmethod
    def _id(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        match = re.search(r"/(?:file/d|folders|document/d|spreadsheets/d|presentation/d|embeddedfolderview)/([A-Za-z0-9_-]+)", parsed.path)
        return match.group(1) if match else urllib.parse.parse_qs(parsed.query).get("id", [""])[0]

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        file_id = cls._id(url)
        if not file_id:
            raise NeedsUser("Please provide an accessible direct Google Drive file or folder link, not the Google Drive homepage.", "user_input")
        if "/folders/" in urllib.parse.urlsplit(url).path or "/embeddedfolderview/" in urllib.parse.urlsplit(url).path:
            return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]

        secrets = secrets or {}
        token = secrets.get("api_token") or secrets.get("access_token") or secrets.get("oauth_token")
        cookie = secrets.get("cookie")
        headers: dict[str, str] = {}
        display_name = f"google-drive-{file_id}"
        size: int | None = None

        if token:
            headers["Authorization"] = f"Bearer {token}"
            direct = f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(file_id)}?alt=media"
            # Try fetching the actual filename and size via Drive API metadata
            try:
                meta = _json(f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(file_id)}?fields=id,name,size", {"api_token": token})
                if meta.get("name"):
                    display_name = meta["name"]
                if meta.get("size") and str(meta["size"]).isdigit():
                    size = int(meta["size"])
            except Exception:
                pass
        else:
            if cookie:
                headers["Cookie"] = str(cookie)
            direct = f"https://drive.usercontent.google.com/download?id={urllib.parse.quote(file_id)}&export=download&confirm=t"

        item = cls._item(url, file_id, display_name, direct, size=size, metadata={"file_id": file_id, "type": "file"})
        item.headers = headers
        return [item]

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed_path = urllib.parse.urlsplit(url).path
        if any(marker in parsed_path for marker in ("/file/d/", "/document/d/", "/spreadsheets/d/", "/presentation/d/")):
            return cls.resolve(url, secrets)
        folder_id = cls._id(url)
        if not folder_id:
            raise NeedsUser("Please provide an accessible direct Google Drive folder link, not the Google Drive homepage.", "user_input")
        secrets = secrets or {}
        token = secrets.get("api_token") or secrets.get("access_token") or secrets.get("oauth_token")

        if token:
            # Authenticated folder listing via Google Drive v3 API using OAuth bearer token
            try:
                query = urllib.parse.quote(f"'{folder_id}' in parents and trashed = false")
                endpoint = f"https://www.googleapis.com/drive/v3/files?q={query}&fields=files(id,name,mimeType,size,parents,webContentLink)"
                data = _json(endpoint, {"api_token": token})
                result = []
                for node in data.get("files", []):
                    is_folder = node.get("mimeType") == "application/vnd.google-apps.folder"
                    node_id = node.get("id", node.get("name", "node"))
                    direct = None if is_folder else f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(str(node_id))}?alt=media"
                    item = cls._item(url, node_id, node.get("name", "node"), direct,
                                     int(node["size"]) if str(node.get("size", "")).isdigit() else None,
                                     metadata={"type": "folder" if is_folder else "file", "parent_id": folder_id, "file_id": node_id})
                    if direct:
                        item.headers = {"Authorization": f"Bearer {token}"}
                    result.append(item)
                if result:
                    return result
            except Exception:
                pass

        api_key = secrets.get("api_key")
        if not api_key:
            # Attempt to discover the public Drive web client API key embedded in Google Drive's folder HTML
            try:
                body, _, _ = _fetch(url, secrets)
                text = body.decode("utf-8", errors="replace")
                discovered = list(dict.fromkeys(re.findall(r"AIzaSy[A-Za-z0-9_-]{33}", text)))
                for cand in discovered:
                    try:
                        query = urllib.parse.quote(f"'{folder_id}' in parents and trashed = false")
                        endpoint = f"https://www.googleapis.com/drive/v3/files?q={query}&fields=files(id,name,mimeType,size,parents,webContentLink)&key={urllib.parse.quote(cand)}"
                        data = _json(endpoint, secrets)
                        api_key = cand
                        break
                    except Exception:
                        continue
            except Exception:
                pass

        if api_key:
            query = urllib.parse.quote(f"'{folder_id}' in parents and trashed = false")
            endpoint = f"https://www.googleapis.com/drive/v3/files?q={query}&fields=files(id,name,mimeType,size,parents,webContentLink)&key={urllib.parse.quote(str(api_key))}"
            data = _json(endpoint, secrets)
            result = []
            for node in data.get("files", []):
                is_folder = node.get("mimeType") == "application/vnd.google-apps.folder"
                node_id = node.get("id", node.get("name", "node"))
                direct = None if is_folder else f"https://drive.usercontent.google.com/download?id={urllib.parse.quote(str(node_id))}&export=download&confirm=t"
                result.append(cls._item(url, node_id, node.get("name", "node"), direct,
                                        int(node["size"]) if str(node.get("size", "")).isdigit() else None,
                                        metadata={"type": "folder" if is_folder else "file", "parent_id": folder_id, "file_id": node_id}))
            return result
        body, final, _ = _fetch(url, secrets)
        links = _links(body, final, ("drive.google.com", "docs.google.com"))
        result = []
        for index, candidate in enumerate(links):
            child = cls._id(candidate)
            if child:
                result.append(cls._item(url, child, f"google-drive-{child}",
                                        None if "/folders/" in candidate else f"https://drive.usercontent.google.com/download?id={child}&export=download&confirm=t",
                                        metadata={"type": "folder" if "/folders/" in candidate else "file", "parent_id": folder_id}))
        if not result:
            raise ProviderUnavailable("Google Drive folder contents require a public listing or API key")
        return result


class PixeldrainProvider(HostedProvider):
    id, hosts = "pixeldrain", ("pixeldrain.com",)

    @staticmethod
    def _parts(url: str) -> tuple[str, bool]:
        parts = [part for part in urllib.parse.urlsplit(url).path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"u", "l"}:
            return parts[1], parts[0] == "l"
        raise ProviderUnavailable("Pixeldrain URL must contain /u/<file> or /l/<list>")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        key, is_list = cls._parts(url)
        if is_list:
            selected = _selected(secrets)
            items = cls.enumerate(url, secrets)
            if selected is not None: items = [item for item in items if item.item_id in selected]
            if not items: raise NeedsUser("No Pixeldrain files are selected", "file_selection")
            resolved: list[ResolvedItem] = []
            for item in items:
                if (item.metadata or {}).get("type") == "file":
                    resolved.extend(cls._resolve_file(item, secrets))
            return resolved
        return cls._resolve_file(cls._item(url, key, key, f"https://pixeldrain.com/api/file/{key}?download", metadata={"type": "file"}), secrets)

    @classmethod
    def _resolve_file(cls, item: ResolvedItem, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        if not item.direct_url and item.item_id:
            item.direct_url = f"https://pixeldrain.com/api/file/{item.item_id}?download"
        try:
            data = _json(f"https://pixeldrain.com/api/file/{item.item_id}/info", secrets)
            info = data.get("value", data)
            item.display_name = _safe_name(info.get("name", item.display_name), item.item_id or "download")
            item.relative_path = item.display_name
            item.size = int(info["size"]) if str(info.get("size", "")).isdigit() else item.size
            item.metadata.update({"type": "file", "checksum": info.get("hash")})
        except ProviderMappedError:
            raise
        except Exception:
            pass
        return [item]

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        key, is_list = cls._parts(url)
        if not is_list:
            return [cls._item(url, key, key, None, metadata={"type": "file", "mode": "tree"})]
        data = _json(f"https://pixeldrain.com/api/list/{key}", secrets)
        result = []
        for index, node in enumerate(data.get("value", data).get("files", [])):
            node_id = str(node.get("id") or node.get("name") or index)
            result.append(cls._item(url, node_id, node.get("name", node_id), None, int(node["size"]) if str(node.get("size", "")).isdigit() else None,
                                    metadata={"type": "file", "parent_id": key, "mode": "tree", "checksum": node.get("hash")}))
        if not result: raise ProviderUnavailable("Pixeldrain list contains no files")
        return result


class OneFichierProvider(HostedProvider):
    id, hosts = "1fichier", ("1fichier.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, headers = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        if re.search(r"password|mot de passe", text, re.I) and not (secrets or {}).get("password"):
            raise NeedsUser("1Fichier link requires a password", "password")
        direct = next((candidate for candidate in _links(body, final, ("1fichier.com",)) if "/dl/" in candidate or "download" in candidate.lower()), None)
        if not direct:
            match = re.search(r"(https?://[^\"']+/dl/[^\"']+)", text, re.I)
            direct = html.unescape(match.group(1)) if match else None
        if not direct: raise ProviderUnavailable("1Fichier download link was not present")
        name = headers.get_filename() or urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1] or "download"
        return [cls._item(url, urllib.parse.urlsplit(url).query or urllib.parse.urlsplit(url).path, name, direct, metadata={"final_url": final, "type": "file"})]


class KrakenfilesProvider(HostedProvider):
    id, hosts = "krakenfiles", ("krakenfiles.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, headers = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        direct = next((candidate for candidate in _links(body, final, ("krakenfiles.com",)) if "/download/" in candidate or ".krakenfiles" in candidate), None)
        if not direct:
            match = re.search(r"(?:download_url|downloadUrl|file_url)\"?\s*[:=]\s*[\"']([^\"']+)", text, re.I)
            direct = urllib.parse.urljoin(final, html.unescape(match.group(1))) if match else None
        if not direct: raise ProviderUnavailable("Krakenfiles download link was not present")
        name = headers.get_filename() or urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1] or "download"
        return [cls._item(url, urllib.parse.urlsplit(final).path, name, direct, metadata={"final_url": final, "type": "file"})]


class CyberdropProvider(HostedProvider):
    id, hosts = "cyberdrop", ("cyberdrop.me", "cyberdrop.to")

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        result = []
        for index, candidate in enumerate(_links(body, final, ("cyberdrop.me", "cyberdrop.to", "cyberdrop.cc"))):
            if any(part in candidate.lower() for part in ("/a/", "/f/", ".jpg", ".jpeg", ".png", ".mp4", ".zip", ".mkv")):
                name = urllib.parse.urlsplit(candidate).path.rsplit("/", 1)[-1] or f"file-{index}"
                result.append(cls._item(url, f"cyberdrop:{index}:{candidate}", name, candidate, metadata={"type": "file", "mode": "tree"}))
        if not result: raise ProviderUnavailable("Cyberdrop page contains no downloadable files")
        return result

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        if re.search(r"/(?:a|album)/", urllib.parse.urlsplit(url).path, re.I):
            selected = _selected(secrets)
            items = cls.enumerate(url, secrets)
            if selected is not None: items = [item for item in items if item.item_id in selected]
            if not items: raise NeedsUser("No Cyberdrop files are selected", "file_selection")
            return items
        body, final, _ = _fetch(url, secrets)
        direct = next((candidate for candidate in _links(body, final, ("cyberdrop.me", "cyberdrop.to", "cyberdrop.cc")) if "/f/" in candidate or "." in urllib.parse.urlsplit(candidate).path.rsplit("/", 1)[-1]), None)
        if not direct: raise ProviderUnavailable("Cyberdrop file link was not present")
        name = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1] or "download"
        return [cls._item(url, urllib.parse.urlsplit(direct).path, name, direct, metadata={"type": "file"})]


BUILTIN_HOSTED_PROVIDERS = [MediaFireProvider, GoogleDriveProvider, PixeldrainProvider,
                            OneFichierProvider, KrakenfilesProvider, CyberdropProvider]
