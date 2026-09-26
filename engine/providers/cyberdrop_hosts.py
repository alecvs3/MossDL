from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

from ..errors import NeedsUser, ProviderMappedError, ProviderUnavailable
from ..models import ResolvedItem
from .hosted import HostedProvider, _fetch, _headers, _json, _links, _safe_name, _selected
from .. import route_http

# Anti-storm circuit: a browser solve that is immediately rejected by the
# provider must not trigger another solve forever. After
# ``_SOLVE_REJECTION_LIMIT`` rejections within ``_SOLVE_REJECTION_WINDOW``
# seconds the host stops solving and surfaces a manual/backoff path instead.
_SOLVE_REJECTION_LIMIT = 8
_SOLVE_REJECTION_WINDOW = 600.0
_solve_rejections: dict[str, list[float]] = {}
_solve_rejections_lock = threading.Lock()


def _solve_backoff_active(host: str) -> bool:
    now = time.time()
    with _solve_rejections_lock:
        stamps = [s for s in _solve_rejections.get(host, []) if now - s < _SOLVE_REJECTION_WINDOW]
        _solve_rejections[host] = stamps
        return len(stamps) >= _SOLVE_REJECTION_LIMIT


def _note_solve_rejection(host: str) -> int:
    with _solve_rejections_lock:
        stamps = _solve_rejections.setdefault(host, [])
        stamps.append(time.time())
        return len(stamps)


def _post(url: str, data: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None,
          headers: dict[str, str] | None = None, secrets: dict[str, Any] | None = None,
          timeout: float = 20.0) -> tuple[bytes, str, Any]:
    from .. import http_client
    req_headers = _headers(secrets)
    if headers:
        req_headers.update(headers)
    try:
        response = http_client.post(url, data=data, json_data=json_body, headers=req_headers, timeout=timeout)
        if response.status in {401, 403}:
            raise ProviderMappedError("provider authentication or access was rejected", "authentication_required", response.status)
        if response.status == 404:
            raise ProviderMappedError("provider item was not found", "not_found", response.status)
        if response.status >= 400:
            raise ProviderUnavailable(f"provider request failed with HTTP {response.status}")
        res_body = response.read(16 * 1024 * 1024 + 1)
        return res_body, response.geturl() or url, response.headers
    except (ProviderMappedError, ProviderUnavailable):
        raise
    except Exception as exc:
        raise ProviderUnavailable(f"provider request failed: {exc}") from exc


def _post_json(url: str, data: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None,
               headers: dict[str, str] | None = None, secrets: dict[str, Any] | None = None,
               timeout: float = 20.0) -> dict[str, Any]:
    body, _, _ = _post(url, data=data, json_body=json_body, headers=headers, secrets=secrets, timeout=timeout)
    try:
        val = json.loads(body.decode("utf-8", "replace"))
        return val if isinstance(val, dict) else {"data": val}
    except Exception as exc:
        raise ProviderUnavailable("provider returned invalid JSON") from exc


OFFLINE_PATTERN = re.compile(
    r"(?i)(?:(?:file\s+(?:not\s+found|was\s+deleted|has\s+been\s+deleted|has\s+been\s+removed|is\s+no\s+longer\s+available|does\s+not\s+exist|expired|has\s+expired|unavailable))|"
    r"(?:could\s+not\s+be\s+found|no\s+such\s+file|link\s+(?:is\s+)?dead|download\s+not\s+found)|"
    r"(?:(?:file\s+)?deleted\s+by\s+(?:administration|its\s+owner|the\s+owner))|"
    r"(?:(?:file\s+)?removed\s+due\s+to\s+copyright|(?:file\s+)?removed\s+by\s+dmca))"
)


def assert_file_online(text: str, host_label: str = "Host") -> None:
    # Strip HTML tags/scripts/links so footer menus like "Report abuse / DMCA" or "Terms" don't trigger false positives
    cleaned = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<a\b[^>]*>.*?</a>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    if OFFLINE_PATTERN.search(cleaned):
        raise ProviderMappedError(f"{host_label}: File not found or deleted on host", "not_found", 404)


# ---------------------------------------------------------------------------

# 1. Vikingfile (vikingfile.com, vik1ngfile.site)
# ---------------------------------------------------------------------------
class VikingfileProvider(HostedProvider):
    id, hosts = "vikingfile", ("vikingfile.com", "vik1ngfile.site")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        secrets = secrets or {}
        parsed = urllib.parse.urlsplit(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if not path_parts:
            raise ProviderUnavailable("Invalid Vikingfile URL")

        # Direct link already resolved
        if path_parts[0] == "d" or "cloudflarestorage.com" in (parsed.hostname or ""):
            name = path_parts[-1] if path_parts else "download"
            return [cls._item(url, parsed.path, name, url, metadata={"type": "file"})]

        file_id = path_parts[1] if len(path_parts) >= 2 and path_parts[0] == "f" else path_parts[-1]
        body, final, _ = _fetch(f"https://vikingfile.com/f/{file_id}", secrets)
        text = body.decode("utf-8", "replace")

        name_match = re.search(r'id=["\']filename["\'][^>]*>([^<]+)<', text)
        size_match = re.search(r'id=["\']size["\'][^>]*>([^<]+)<', text)
        filename = name_match.group(1).strip() if name_match else f"viking_{file_id}"
        size_str = size_match.group(1).strip() if size_match else ""

        # Check turnstile token in secrets
        token = secrets.get("turnstile_token") or secrets.get("cf-turnstile-response")
        if not token and isinstance(secrets.get("captcha_solution"), dict):
            sol = secrets["captcha_solution"]
            token = sol.get("token") or sol.get("cf-turnstile-response")
            cand_url = sol.get("link") or sol.get("direct_link") or sol.get("url") or sol.get("direct")
            if cand_url and isinstance(cand_url, str) and (cand_url.startswith("http://") or cand_url.startswith("https://")):
                return [cls._item(url, file_id, filename, cand_url, metadata={"type": "file", "size_str": size_str})]

        if token and (token.startswith("http://") or token.startswith("https://")):
            return [cls._item(url, file_id, filename, token, metadata={"type": "file", "size_str": size_str})]

        if token and token != "flaresolverr_ok":
            for host in ("vik1ngfile.site", "vikingfile.com"):
                try:
                    data = _post_json(f"https://{host}/f/{file_id}",
                                      data={"cf-turnstile-response": token},
                                      headers={"Referer": f"https://{host}/f/{file_id}"},
                                      secrets=secrets)
                    direct = data.get("direct-link") or data.get("link") or data.get("url")
                    if direct:
                        return [cls._item(url, file_id, filename, direct, metadata={"type": "file", "size_str": size_str})]
                except Exception:
                    pass

        # Check if direct link was present in page HTML
        direct_match = re.search(r'href=["\']((https?://[^"\']*(?:cloudflarestorage\.com|/d/)[^"\']+))["\']', text)
        if direct_match:
            return [cls._item(url, file_id, filename, direct_match.group(1), metadata={"type": "file"})]

        sitekey_match = re.search(r"""(?:sitekey|data-sitekey)\s*[:=]\s*['"]([^'"]+)['"]""", text)
        sitekey = sitekey_match.group(1) if sitekey_match else ""

        task_id = secrets.get("task_id")
        pfx = f"[{task_id[:8]}] " if task_id else ""
        t_ctx = {"task_id": task_id} if task_id else {}

        # Attempt automated headless solve before requesting user intervention
        if "challenges.cloudflare.com" in text or "cf-turnstile" in text or "turnstile" in text.lower():
            try:
                from ..browser_solver import solver_daemon
                from ..telemetry import telemetry_bus
                telemetry_bus.record(
                    level="INFO", subsystem="engine:captcha",
                    message=f"{pfx}[CAPTCHA_DETECTED] Cloudflare Turnstile detected on vikingfile.com — solving in background with automated browser...",
                    context={**t_ctx, "file_id": file_id, "url": url, "sitekey": sitekey},
                    tier="engine",
                )
                solve_res = solver_daemon.solve_challenge_sync(
                    f"https://vikingfile.com/f/{file_id}", timeout_seconds=25.0, task_id=task_id,
                )
                if solve_res.get("success"):
                    # Priority 1: Direct URL captured via response interception or DOM
                    direct = solve_res.get("direct_url")
                    if direct and isinstance(direct, str) and direct.startswith("http"):
                        dur_s = (solve_res.get("duration_ms") or 0) / 1000.0
                        telemetry_bus.record(
                            level="INFO", subsystem="engine:captcha",
                            message=f"{pfx}[CAPTCHA_BYPASSED] Challenge solved in {dur_s:.1f}s via {solve_res.get('engine')} — direct download link acquired",
                            context={**t_ctx, "direct_url": direct[:120], "engine": solve_res.get("engine"),
                                     "duration_ms": solve_res.get("duration_ms")},
                            tier="engine",
                        )
                        item = cls._item(url, file_id, filename, direct, metadata={"type": "file", "size_str": size_str})
                        if solve_res.get("cookies"):
                            item.cookies = dict(solve_res["cookies"])
                        return [item]

                    # Priority 2: Turnstile token — POST it to get download link
                    auto_token = solve_res.get("turnstile_token")
                    if auto_token:
                        for host in ("vik1ngfile.site", "vikingfile.com"):
                            try:
                                data = _post_json(f"https://{host}/f/{file_id}",
                                                  data={"cf-turnstile-response": auto_token},
                                                  headers={"Referer": f"https://{host}/f/{file_id}"},
                                                  secrets=secrets)
                                direct = data.get("direct-link") or data.get("link") or data.get("url")
                                if direct:
                                    dur_s = (solve_res.get("duration_ms") or 0) / 1000.0
                                    telemetry_bus.record(
                                        level="INFO", subsystem="engine:captcha",
                                        message=f"{pfx}[CAPTCHA_BYPASSED] Challenge solved in {dur_s:.1f}s via {solve_res.get('engine')} (token exchange) — direct link acquired",
                                        context={**t_ctx, "direct_url": direct[:120], "engine": solve_res.get("engine")},
                                        tier="engine",
                                    )
                                    item = cls._item(url, file_id, filename, direct, metadata={"type": "file", "size_str": size_str})
                                    if solve_res.get("cookies"):
                                        item.cookies = dict(solve_res["cookies"])
                                    return [item]
                            except Exception:
                                pass
                else:
                    telemetry_bus.record(
                        level="WARN", subsystem="engine:captcha",
                        message=f"{pfx}[VIKINGFILE_AUTO_FAIL] Automated solver did not resolve: {solve_res.get('error', 'unknown')}",
                        context={**t_ctx, "engine": solve_res.get("engine"), "error": solve_res.get("error")},
                        tier="engine",
                    )
            except Exception as e:
                logger.debug("Automated solver on VikingFile failed: %s", e)

        raise NeedsUser("Cloudflare Turnstile verification required for Vikingfile", "turnstile",
                        {"sitekey": sitekey, "site_key": sitekey, "url": f"https://vikingfile.com/f/{file_id}",
                         "page_url": f"https://vikingfile.com/f/{file_id}", "file_id": file_id, "filename": filename,
                         "captcha_type": "turnstile"})


# ---------------------------------------------------------------------------
# 2. Filester (filester.me)
# ---------------------------------------------------------------------------
class FilesterProvider(HostedProvider):
    id, hosts = "filester", ("filester.me",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0] == "f":
            return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]
        slug = parts[-1] if parts else ""
        data = _post_json("https://filester.me/v2/api/public/download", json_body={"file_slug": slug}, secrets=secrets)
        server = data.get("server", "").rstrip("/")
        file_path = data.get("file", "").lstrip("/")
        token = data.get("token", "")
        if server and file_path and token:
            direct = f"{server}/v2/{file_path}?token={token}&download=true"
            fname = file_path.rsplit("/", 1)[-1]
            return [cls._item(url, slug, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Filester download URL could not be generated")

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        items: list[ResolvedItem] = []
        for match in re.finditer(r'class=["\'][^"\']*file-item[^"\']*["\'][^>]*onclick=["\'][^"\']*[\'"]([A-Za-z0-9_-]+)[\'"]', text):
            slug = match.group(1)
            items.append(cls._item(url, slug, f"filester-{slug}", f"https://filester.me/d/{slug}", metadata={"type": "file", "mode": "tree"}))
        for match in re.finditer(r'href=["\'](/f/[^"\']+)["\']', text):
            subpath = match.group(1)
            sf_slug = subpath.rsplit("/", 1)[-1]
            items.append(cls._item(url, f"folder:{sf_slug}", sf_slug, None, metadata={"type": "folder", "mode": "tree"}))
        if not items:
            return cls.resolve(url, secrets)
        return items


# ---------------------------------------------------------------------------
# 3. Koofr (koofr.eu, koofr.net, k00.fr)
# ---------------------------------------------------------------------------
class KoofrProvider(HostedProvider):
    id, hosts = "koofr", ("koofr.eu", "koofr.net", "k00.fr", "app.koofr.net")

    @staticmethod
    def _cid(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        parts = [p for p in parsed.path.split("/") if p]
        return parts[-1] if parts else ""

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        cid = cls._cid(url)
        info = _json(f"https://app.koofr.net/api/v2/public/links/{cid}", secrets)
        if info.get("type") == "file":
            fname = info.get("name", cid)
            direct = f"https://app.koofr.net/content/links/{cid}/files/get/{urllib.parse.quote(fname)}"
            return [cls._item(url, cid, fname, direct, info.get("size"), metadata={"type": "file"})]
        return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        cid = cls._cid(url)
        items: list[ResolvedItem] = []

        def walk(path: str = "/"):
            data = _json(f"https://app.koofr.net/api/v2/public/links/{cid}/files/list?path={urllib.parse.quote(path)}", secrets)
            for file_node in data.get("files", []):
                fname = file_node.get("name", "file")
                is_dir = file_node.get("type") == "dir"
                fpath = f"{path.rstrip('/')}/{fname}"
                if is_dir:
                    items.append(cls._item(url, f"folder:{fpath}", fname, None, metadata={"type": "folder", "mode": "tree"}))
                    walk(fpath)
                else:
                    direct = f"https://app.koofr.net/content/links/{cid}/files/get/{urllib.parse.quote(fname)}?path={urllib.parse.quote(fpath)}"
                    items.append(cls._item(url, fpath, fname, direct, file_node.get("size"), relative=fpath.lstrip("/"),
                                           metadata={"type": "file", "mode": "tree"}))

        try:
            walk("/")
        except Exception:
            pass
        return items or cls.resolve(url, secrets)


# ---------------------------------------------------------------------------
# 4. Fileditch / TheDitch (fileditch.com, fileditchfiles.me, theditch.st)
# ---------------------------------------------------------------------------
class FileditchProvider(HostedProvider):
    id, hosts = "fileditch", ("fileditch.com", "fileditchfiles.me", "theditch.st")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        # Direct cdn link
        if any(p in parsed.path for p in ("/temp/", "/beta123/", "/alpha7/")):
            fname = parsed.path.rsplit("/", 1)[-1]
            return [cls._item(url, parsed.path, fname, url, metadata={"type": "file"})]

        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        btn = re.search(r'href=["\'](https?://[^"\']+)["\'][^>]*class=["\'][^"\']*btn-main[^"\']*["\']', text) or \
              re.search(r'class=["\'][^"\']*btn-main[^"\']*["\'][^>]*href=["\'](https?://[^"\']+)["\']', text) or \
              re.search(r'id=["\']fd-go["\'][^>]*href=["\'](https?://[^"\']+)["\']', text)
        if btn:
            direct = html.unescape(btn.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, parsed.path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Fileditch direct link was not found")


# ---------------------------------------------------------------------------
# 5 & 6. YetiShare Base (Iceyfile, Cyberfile)
# ---------------------------------------------------------------------------
class YetiShareBaseProvider(HostedProvider):
    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, headers = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        # Check download token onclick or button
        match = re.search(r'''(?:href|onclick)=["']([^"']*(?:download_token|file_details)[^"']*)["']''', text, re.I)
        if match:
            candidate = urllib.parse.urljoin(final, html.unescape(match.group(1)))
            fname = headers.get_filename() or urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1] or "download"
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, candidate, metadata={"type": "file"})]

        # Check generic download link in page
        for link in _links(body, final):
            if "download" in link.lower() and not link.endswith((".css", ".js")):
                fname = urllib.parse.urlsplit(link).path.rsplit("/", 1)[-1]
                return [cls._item(url, urllib.parse.urlsplit(final).path, fname, link, metadata={"type": "file"})]
        raise ProviderUnavailable(f"{cls.id} download link could not be located")

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        items: list[ResolvedItem] = []
        for m in re.finditer(r'class=["\'][^"\']*fileItem[^"\']*["\'][^>]*fileid=["\']([^"\']+)["\']', text):
            fid = m.group(1)
            items.append(cls._item(url, fid, f"file-{fid}", None, metadata={"type": "file", "mode": "tree"}))
        return items or cls.resolve(url, secrets)


class IceyfileProvider(YetiShareBaseProvider):
    id, hosts = "iceyfile", ("iceyfile.com",)


class CyberfileProvider(YetiShareBaseProvider):
    id, hosts = "cyberfile", ("cyberfile.me",)


# ---------------------------------------------------------------------------
# 7, 8, 9. Chevereto Base (ImgLike, ImagePond, ImgBB)
# ---------------------------------------------------------------------------
class CheveretoBaseProvider(HostedProvider):
    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        # Check image viewer
        img_match = re.search(r'id=["\']image-viewer["\'][^>]*>.*?<img[^>]+src=["\']([^"\']+)["\']', text, re.DOTALL | re.I) or \
                    re.search(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*id=["\']image-viewer-full["\']', text, re.I) or \
                    re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', text, re.I)
        if img_match:
            direct = html.unescape(img_match.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable(f"{cls.id} image link was not found")

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        items: list[ResolvedItem] = []
        for m in re.finditer(r'<a[^>]+class=["\'][^"\']*image-container[^"\']*["\'][^>]+href=["\']([^"\']+)["\']', text):
            cand = urllib.parse.urljoin(final, html.unescape(m.group(1)))
            fname = urllib.parse.urlsplit(cand).path.rsplit("/", 1)[-1]
            items.append(cls._item(url, cand, fname, cand, metadata={"type": "file", "mode": "tree"}))
        return items or cls.resolve(url, secrets)


class ImgLikeProvider(CheveretoBaseProvider):
    id, hosts = "imglike", ("imglike.com",)


class ImagePondProvider(CheveretoBaseProvider):
    id, hosts = "imagepond", ("imagepond.net",)


class ImgBBProvider(CheveretoBaseProvider):
    id, hosts = "imgbb", ("imgbb.com", "ibb.co")


# ---------------------------------------------------------------------------
# 10. Catbox & Litterbox (catbox.moe)
# ---------------------------------------------------------------------------
class CatboxProvider(HostedProvider):
    id, hosts = "catbox", ("catbox.moe", "litter.catbox.moe", "files.catbox.moe", "fatbox.moe")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        fname = parsed.path.rsplit("/", 1)[-1] or "catbox-download"
        return [cls._item(url, parsed.path, fname, url, metadata={"type": "file"})]


# ---------------------------------------------------------------------------
# 11. Upload.ee (upload.ee)
# ---------------------------------------------------------------------------
class UploadEEProvider(HostedProvider):
    id, hosts = "upload_ee", ("upload.ee", "www.upload.ee")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        dl = re.search(r'id=["\']d_l["\'][^>]*href=["\']([^"\']+)["\']', text) or \
             re.search(r'href=["\']([^"\']+)["\'][^>]*id=["\']d_l["\']', text)
        if dl:
            direct = html.unescape(dl.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Upload.ee download link not found")


# ---------------------------------------------------------------------------
# 12. Bunkr (bunkr.cr, bunkr.site, bunkr.ph, bunkr.is, etc.)
# ---------------------------------------------------------------------------
class BunkrProvider(HostedProvider):
    id = "bunkr"
    hosts = ("bunkr.cr", "bunkr.site", "bunkr.ph", "bunkr.is", "bunkr.black", "bunkr.media", "bunkr.red", "bunkr.ws")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        if "/a/" in url:
            return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        dl = re.search(r'href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*ic-download-01[^"\']*["\']', text) or \
             re.search(r'class=["\'][^"\']*ic-download-01[^"\']*["\'][^>]*href=["\']([^"\']+)["\']', text)
        if dl:
            direct = html.unescape(dl.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        # Check video/source
        src = re.search(r'<source[^>]+src=["\']([^"\']+)["\']', text)
        if src:
            direct = html.unescape(src.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Bunkr file download link not found")

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        album_match = re.search(r'window\.albumFiles\s*=\s*(\[[^\]]+\]);', text)
        items: list[ResolvedItem] = []
        if album_match:
            try:
                files = json.loads(album_match.group(1))
                for f in files:
                    fname = f.get("name") or "file"
                    furl = f.get("cdn") or f.get("url")
                    items.append(cls._item(url, f.get("id", fname), fname, furl, f.get("size"),
                                           metadata={"type": "file", "mode": "tree"}))
                return items
            except Exception:
                pass
        for m in re.finditer(r'href=["\'](/(?:v|f|d|i)/[^"\']+)["\']', text):
            child_url = urllib.parse.urljoin(final, m.group(1))
            fname = child_url.rsplit("/", 1)[-1]
            items.append(cls._item(url, child_url, fname, child_url, metadata={"type": "file", "mode": "tree"}))
        return items or cls.resolve(url, secrets)


# ---------------------------------------------------------------------------
# 13. AnonTransfer (anontransfer.com)
# ---------------------------------------------------------------------------
class AnonTransferProvider(HostedProvider):
    id, hosts = "anontransfer", ("anontransfer.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        dl = re.search(r'href=["\']([^"\']*/(?:download|uploads)/[^"\']+)["\']', text, re.I)
        if dl:
            direct = urllib.parse.urljoin(final, html.unescape(dl.group(1)))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("AnonTransfer download link not found")


# ---------------------------------------------------------------------------
# 14. PCloud (pcloud.com, pcloud.link, pc.cd, e.pc.cd, u.pc.cd)
# ---------------------------------------------------------------------------
class PCloudProvider(HostedProvider):
    id, hosts = "pcloud", ("pcloud.com", "pcloud.link", "pc.cd", "e.pc.cd", "u.pc.cd")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        code = parsed.path.strip("/")
        if "code=" in parsed.query:
            code = urllib.parse.parse_qs(parsed.query).get("code", [code])[0]
        data = _json(f"https://api.pcloud.com/showpublink?code={code}", secrets)
        metadata = data.get("metadata", {})
        if metadata.get("isfolder"):
            return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]
        hosts_list = data.get("hosts", ["api.pcloud.com"])
        path = data.get("path", "")
        direct = f"https://{hosts_list[0]}{path}" if path else url
        fname = metadata.get("name", code)
        return [cls._item(url, code, fname, direct, metadata.get("size"), metadata={"type": "file"})]

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        code = parsed.path.strip("/")
        data = _json(f"https://api.pcloud.com/showpublink?code={code}", secrets)
        items: list[ResolvedItem] = []

        def walk(node: dict[str, Any], path_prefix: str = ""):
            is_folder = node.get("isfolder", False)
            name = node.get("name", "item")
            rel = f"{path_prefix}/{name}".strip("/")
            if is_folder:
                items.append(cls._item(url, f"folder:{rel}", name, None, metadata={"type": "folder", "mode": "tree"}))
                for child in node.get("contents", []):
                    walk(child, rel)
            else:
                items.append(cls._item(url, str(node.get("fileid", rel)), name, None, node.get("size"), relative=rel,
                                       metadata={"type": "file", "mode": "tree"}))

        metadata = data.get("metadata", {})
        walk(metadata)
        return items or cls.resolve(url, secrets)


# ---------------------------------------------------------------------------
# 15. OneDrive (1drv.ms, onedrive.live.com)
# ---------------------------------------------------------------------------
class OneDriveProvider(HostedProvider):
    id, hosts = "onedrive", ("1drv.ms", "onedrive.live.com")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        # OneDrive direct download param is &download=1
        parsed = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qs(parsed.query)
        qs["download"] = ["1"]
        direct = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(qs, doseq=True), parsed.fragment))
        name = parsed.path.rsplit("/", 1)[-1] or "onedrive-file"
        return [cls._item(url, parsed.path, name, direct, metadata={"type": "file"})]


# ---------------------------------------------------------------------------
# 16. Dropbox (dropbox.com)
# ---------------------------------------------------------------------------
class DropboxProvider(HostedProvider):
    id, hosts = "dropbox", ("dropbox.com", "www.dropbox.com")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qs(parsed.query)
        qs["dl"] = ["1"]
        direct = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(qs, doseq=True), parsed.fragment))
        fname = parsed.path.rsplit("/", 1)[-1] or "dropbox-download"
        return [cls._item(url, parsed.path, fname, direct, metadata={"type": "file"})]


# ---------------------------------------------------------------------------
# 17. Box (box.com, app.box.com)
# ---------------------------------------------------------------------------
class BoxProvider(HostedProvider):
    id, hosts = "box", ("box.com", "app.box.com")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        dl = re.search(r'href=["\']([^"\']*/shared/static/[^"\']+)["\']', text)
        if dl:
            direct = html.unescape(dl.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Box direct download link not found")


# ---------------------------------------------------------------------------
# 18. PostImg (postimg.cc, postimg.org, postimages.org, i.postimg.cc)
# ---------------------------------------------------------------------------
class PostImgProvider(HostedProvider):
    id, hosts = "postimg", ("postimg.cc", "postimg.org", "postimages.org", "i.postimg.cc")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        if "i.postimg.cc" in url:
            fname = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
            return [cls._item(url, url, fname, url, metadata={"type": "file"})]
        if "/gallery/" in url:
            return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        dl = re.search(r'id=["\']download["\'][^>]*href=["\']([^"\']+)["\']', text) or \
             re.search(r'href=["\']([^"\']+)["\'][^>]*id=["\']download["\']', text) or \
             re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', text)
        if dl:
            direct = html.unescape(dl.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("PostImg image link not found")

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        album_id = [p for p in parsed.path.split("/") if p][-1]
        try:
            resp = _post_json("https://postimg.cc/json", data={"action": "list", "album": album_id, "page": 1}, secrets=secrets)
            images = resp.get("images", [])
            items = []
            for img in images:
                link = f"https://postimg.cc/{img[0]}"
                items.append(cls._item(url, img[0], img[0], link, metadata={"type": "file", "mode": "tree"}))
            if items:
                return items
        except Exception:
            pass
        return cls.resolve(url, secrets)


# ---------------------------------------------------------------------------
# 19. ImgBox (imgbox.com)
# ---------------------------------------------------------------------------
class ImgBoxProvider(HostedProvider):
    id, hosts = "imgbox", ("imgbox.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        if "/g/" in url:
            return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'id=["\']img["\'][^>]*src=["\']([^"\']+)["\']', text) or \
            re.search(r'src=["\']([^"\']*(?:images2\.imgbox\.com|imgbox\.com)[^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("ImgBox image not found")

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        items = []
        for m in re.finditer(r'href=["\'](/[A-Za-z0-9_-]+)["\'][^>]*>.*?<img', text, re.DOTALL):
            child_url = urllib.parse.urljoin(final, m.group(1))
            fname = child_url.rsplit("/", 1)[-1]
            items.append(cls._item(url, child_url, fname, child_url, metadata={"type": "file", "mode": "tree"}))
        return items or cls.resolve(url, secrets)


# ---------------------------------------------------------------------------
# 20. ImageBam (imagebam.com)
# ---------------------------------------------------------------------------
class ImageBamProvider(HostedProvider):
    id, hosts = "imagebam", ("imagebam.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'class=["\'][^"\']*main-image[^"\']*["\'][^>]*src=["\']([^"\']+)["\']', text) or \
            re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("ImageBam image not found")


# ---------------------------------------------------------------------------
# 21. Imx.to (imx.to)
# ---------------------------------------------------------------------------
class ImxToProvider(HostedProvider):
    id, hosts = "imx_to", ("imx.to",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'class=["\'][^"\']*centred[^"\']*["\'][^>]*src=["\']([^"\']+)["\']', text) or \
            re.search(r'src=["\']([^"\']*(?:imx\.to/upload|imx\.to/i/)[^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Imx.to image not found")


# ---------------------------------------------------------------------------
# 22. ImageVenue (imagevenue.com)
# ---------------------------------------------------------------------------
class ImageVenueProvider(HostedProvider):
    id, hosts = "imagevenue", ("imagevenue.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'id=["\']main-image["\'][^>]*src=["\']([^"\']+)["\']', text) or \
            re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("ImageVenue image not found")


# ---------------------------------------------------------------------------
# 23. PixHost (pixhost.to, pixhost.cc, pixhost.org)
# ---------------------------------------------------------------------------
class PixHostProvider(HostedProvider):
    id, hosts = "pixhost", ("pixhost.to", "pixhost.cc", "pixhost.org")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        if "/gallery/" in url:
            return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'id=["\']image["\'][^>]*src=["\']([^"\']+)["\']', text) or \
            re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("PixHost image not found")

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        items = []
        for m in re.finditer(r'href=["\'](https?://[^"\']*(?:pixhost\.(?:to|cc|org))/show/[^"\']+)["\']', text):
            child = html.unescape(m.group(1))
            fname = child.rsplit("/", 1)[-1]
            items.append(cls._item(url, child, fname, child, metadata={"type": "file", "mode": "tree"}))
        return items or cls.resolve(url, secrets)


# ---------------------------------------------------------------------------
# 24. Imgur (imgur.com, i.imgur.com)
# ---------------------------------------------------------------------------
class ImgurProvider(HostedProvider):
    id, hosts = "imgur", ("imgur.com", "i.imgur.com")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        if "i.imgur.com" in url:
            fname = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
            return [cls._item(url, url, fname, url, metadata={"type": "file"})]
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', text) or \
            re.search(r'property=["\']og:video["\'][^>]*content=["\']([^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Imgur media not found")


# ---------------------------------------------------------------------------
# 28. Streamable (streamable.com)
# ---------------------------------------------------------------------------
class StreamableProvider(HostedProvider):
    id, hosts = "streamable", ("streamable.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        shortcode = urllib.parse.urlsplit(url).path.strip("/").split("/")[-1]
        data = _json(f"https://api.streamable.com/videos/{shortcode}", secrets)
        files = data.get("files", {})
        mp4_info = files.get("mp4") or files.get("mp4-mobile") or next(iter(files.values()), {})
        direct = mp4_info.get("url")
        if direct:
            if direct.startswith("//"):
                direct = "https:" + direct
            fname = data.get("title") or f"{shortcode}.mp4"
            if not fname.endswith(".mp4"):
                fname += ".mp4"
            return [cls._item(url, shortcode, fname, direct, mp4_info.get("size"), metadata={"type": "file"})]
        raise ProviderUnavailable("Streamable direct video URL not found")


# ---------------------------------------------------------------------------
# 30. SendVid (sendvid.com)
# ---------------------------------------------------------------------------
class SendVidProvider(HostedProvider):
    id, hosts = "sendvid", ("sendvid.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'id=["\']video_source["\'][^>]*src=["\']([^"\']+)["\']', text) or \
            re.search(r'property=["\']og:video:url["\'][^>]*content=["\']([^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("SendVid video not found")


# ---------------------------------------------------------------------------
# 32. Whyp.it (whyp.it)
# ---------------------------------------------------------------------------
class WhypItProvider(HostedProvider):
    id, hosts = "whyp_it", ("whyp.it",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'audio_url\s*[:=]\s*["\']([^"\']+)["\']', text) or \
            re.search(r'<audio[^>]+src=["\']([^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Whyp.it audio track not found")


# ---------------------------------------------------------------------------
# 33. BuzzHeavier (buzzheavier.com)
# ---------------------------------------------------------------------------
class BuzzHeavierProvider(HostedProvider):
    id, hosts = "buzzheavier", ("buzzheavier.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        hx = re.search(r'class=["\'][^"\']*download-btn[^"\']*["\'][^>]*hx-get=["\']([^"\']+)["\']', text)
        if hx:
            hx_url = urllib.parse.urljoin(final, html.unescape(hx.group(1)))
            # Request HEAD or GET with HX-Request: true
            req = urllib.request.Request(hx_url, headers={**_headers(secrets), "HX-Request": "true", "HX-Current-URL": url})
            try:
                with route_http.urlopen(req, timeout=15) as resp:
                    direct = resp.headers.get("hx-redirect") or resp.geturl()
                    fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1] or "download"
                    return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
            except Exception:
                pass
        raise ProviderUnavailable("BuzzHeavier download link not found")


# ---------------------------------------------------------------------------
# 34. PillowCase (pillowcase.su, pillowcase.zip, pillows.su)
# ---------------------------------------------------------------------------
class PillowCaseProvider(HostedProvider):
    id, hosts = "pillowcase", ("pillowcase.su", "pillowcase.zip", "pillows.su")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        fid = [p for p in parsed.path.split("/") if p][-1]
        direct = f"https://api.pillows.su/api/download/{fid}"
        return [cls._item(url, fid, f"pillowcase-{fid}", direct, metadata={"type": "file"})]


# ---------------------------------------------------------------------------
# 35. Mixdrop (mixdrop.co, mixdrop.to, mixdrop.sx, mixdrop.bz, etc.)
# ---------------------------------------------------------------------------
class MixdropProvider(HostedProvider):
    id, hosts = "mixdrop", ("mixdrop.co", "mixdrop.to", "mixdrop.sx", "mixdrop.bz", "mixdrop.ch", "mixdrop.ag")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'(?:wurl|surl)\s*=\s*["\']([^"\']+)["\']', text) or \
            re.search(r'MDCore\.wurl\s*=\s*["\']([^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            if direct.startswith("//"):
                direct = "https:" + direct
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Mixdrop video link not found")


# ---------------------------------------------------------------------------
# 36. Doodstream (doodstream.com, dood.so, dood.to, dood.la, doods.pro, etc.)
# ---------------------------------------------------------------------------
class DoodstreamProvider(HostedProvider):
    id, hosts = "doodstream", ("doodstream.com", "dood.so", "dood.to", "dood.la", "dood.ws", "dood.sh", "doods.pro")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'/pass_md5/[^"\']+', text)
        if m:
            pass_url = urllib.parse.urljoin(final, m.group(0))
            pass_body, _, _ = _fetch(pass_url, {**(secrets or {}), "referer": final})
            direct = pass_body.decode("utf-8", "replace").strip() + "z" * 10
            fname = urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1] or "video.mp4"
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Doodstream stream link not found")


# ---------------------------------------------------------------------------
# 37. Streamtape (streamtape.com, streamtape.to, streamtape.net)
# ---------------------------------------------------------------------------
class StreamtapeProvider(HostedProvider):
    id, hosts = "streamtape", ("streamtape.com", "streamtape.to", "streamtape.net", "streamta.pe")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'id=["\']videolink["\'][^>]*>([^<]+)<', text)
        if m:
            raw = m.group(1).strip()
            tok_match = re.search(r"""document\.getElementById\(['"]videolink['"]\)\.innerHTML\s*=\s*.*?['"]&token=([^'"]+)['"]""", text)
            token = tok_match.group(1) if tok_match else ""
            direct = "https:" + raw + (f"&token={token}" if token and "&token=" not in raw else "")
            fname = urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1] or "video.mp4"
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Streamtape media link not found")


# ---------------------------------------------------------------------------
# 38. Voe (voe.sx)
# ---------------------------------------------------------------------------
class VoeProvider(HostedProvider):
    id, hosts = "voe", ("voe.sx",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'["\']hls["\']\s*:\s*["\']([^"\']+)["\']', text) or \
            re.search(r'["\']mp4["\']\s*:\s*["\']([^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1] or "video.mp4"
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Voe media not found")


# ---------------------------------------------------------------------------
# 39. FuckingFast (fuckingfast.co)
# ---------------------------------------------------------------------------
class FuckingFastProvider(HostedProvider):
    id, hosts = "fuckingfast", ("fuckingfast.co",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        secrets = secrets or {}
        parsed = urllib.parse.urlsplit(url)
        fragment_name = parsed.fragment.strip() if parsed.fragment else None

        text = ""
        final = url
        req_headers = _headers(secrets)
        try:
            from curl_cffi import requests as cffi_requests
            r = cffi_requests.get(url, headers=req_headers, impersonate="chrome", timeout=15, proxies=route_http.curl_proxies())
            if r.status_code == 404:
                raise ProviderMappedError("FuckingFast: File not found", "not_found", 404)
            text = r.text
            final = str(r.url)
        except ProviderMappedError:
            raise
        except Exception:
            pass

        if not text or "challenges.cloudflare.com" in text or "Just a moment..." in text:
            try:
                body, final, _ = _fetch(url, secrets)
                text = body.decode("utf-8", "replace")
            except (ProviderMappedError, Exception) as e:
                if "403" in str(e) or "turnstile" in str(e).lower() or "challenge" in str(e).lower() or getattr(e, "status_code", None) == 403:
                    raise NeedsUser("FuckingFast requires Cloudflare Turnstile clearance", "turnstile", {"url": url, "host": "fuckingfast.co"}) from e
                raise

        assert_file_online(text, "FuckingFast")

        hx = re.search(r'class=["\'][^"\']*link-button[^"\']*["\'][^>]*hx-post=["\']([^"\']+)["\']', text) or \
             re.search(r'hx-post=["\']([^"\']*(?:/go|/dl/)[^"\']*)["\']', text)
        if hx:
            hx_url = urllib.parse.urljoin(final, html.unescape(hx.group(1)))
            direct = None
            hx_headers = {**req_headers, "HX-Request": "true", "HX-Current-URL": url, "Referer": url}
            try:
                from curl_cffi import requests as cffi_requests
                post_r = cffi_requests.post(hx_url, headers=hx_headers, impersonate="chrome", timeout=15, proxies=route_http.curl_proxies())
                direct = post_r.headers.get("hx-redirect") or post_r.headers.get("location")
            except Exception:
                pass
            if not direct:
                req = urllib.request.Request(hx_url, data=b"", headers=hx_headers, method="POST")
                try:
                    with route_http.urlopen(req, timeout=15) as resp:
                        direct = resp.headers.get("hx-redirect") or resp.headers.get("location") or resp.geturl()
                except Exception:
                    pass

            if direct:
                fname = fragment_name or urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1] or "download.zip"
                return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]

        dl_script = re.search(r'https://(?:dl\.)?fuckingfast\.co/dl/[A-Za-z0-9_\-]+', text)
        if dl_script:
            direct = dl_script.group(0)
            fname = fragment_name or urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1] or "download.zip"
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]

        if "challenges.cloudflare.com" in text or "Just a moment..." in text:
            raise NeedsUser("FuckingFast requires Cloudflare Turnstile clearance", "turnstile", {"url": url, "host": "fuckingfast.co"})
        raise ProviderUnavailable("FuckingFast download link not found")


# ---------------------------------------------------------------------------
# 39b. DataNodes (datanodes.to)
# ---------------------------------------------------------------------------
class DatanodesProvider(HostedProvider):
    id, hosts = "datanodes", ("datanodes.to",)

    @staticmethod
    def _fname_candidates(html: str) -> list[str]:
        return re.findall(r'name=["\']fname["\']\s+value=["\']([^"\']+)["\']', html)

    @classmethod
    def _select_step1_fname(cls, html: str, file_name: str) -> str:
        """Pick the real ``fname`` input, ignoring DataNodes' decoy.

        DataNodes injects a decoy hidden input before the real one
        (``<input name="fname" value="Download">``) so a naive first-match
        scrape sends ``fname="Download"`` and the step-1 POST is rejected,
        which prevents step 2 (countdown/rand/dl-token) from ever loading.
        """
        candidates = cls._fname_candidates(html)
        if file_name and file_name in candidates:
            return file_name
        real_names = [c for c in candidates if c.strip().lower() not in ("", "download")]
        if real_names:
            return real_names[-1]
        if file_name:
            return file_name
        if candidates:
            return candidates[-1]
        return file_name

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        secrets = secrets or {}
        parsed = urllib.parse.urlsplit(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if not path_parts:
            raise ProviderUnavailable("Invalid DataNodes URL")

        file_code = path_parts[0]
        file_name = path_parts[1] if len(path_parts) > 1 else f"datanodes_{file_code}.rar"
        if len(path_parts) >= 3 and path_parts[0] in ("d", "f"):
            file_code = path_parts[1]
            file_name = path_parts[2]

        task_id = secrets.get("task_id")
        pfx = f"[{task_id[:8]}] " if task_id else ""
        t_ctx = {"task_id": task_id} if task_id else {}

        def _trace(message: str, level: str = "INFO", **ctx: Any) -> None:
            try:
                from ..telemetry import telemetry_bus
                telemetry_bus.record(
                    level=level, subsystem="engine:resolve",
                    message=f"{pfx}[DN] {message}", context={**t_ctx, **ctx}, tier="engine",
                )
            except Exception:
                pass

        # A challenge can suspend this operation after step one. The engine keeps
        # the opaque state in memory and returns it only to this provider on the
        # exact task retry, allowing step two to continue in the original session.
        continuation = secrets.get("_provider_continuation")
        if continuation is not None:
            from .datanodes_resume import resume as resume_datanodes, validate_continuation
            continuation_valid, continuation_reason = validate_continuation(
                continuation, url=url, file_code=file_code,
            )
            if continuation_valid:
                try:
                    direct, resume_cookies, resume_size = resume_datanodes(
                        url=url, file_code=file_code, file_name=file_name,
                        secrets=secrets, state=continuation, trace=_trace,
                    )
                except ProviderUnavailable as exc:
                    if type(exc) is not ProviderUnavailable:
                        raise
                    _trace("continuation_invalidated", level="WARN", reason=str(exc))
                else:
                    result = cls._item(url, file_code, file_name, direct, size=resume_size,
                                       metadata={"type": "file"})
                    result.cookies = resume_cookies
                    return [result]
            else:
                _trace("continuation_invalidated", level="WARN", reason=continuation_reason)
            try:
                from ..critical_trace import mark
                excluded = ("url", "code", "name", "body", "snippet", "candidate", "chosen",
                            "rand", "token", "cookie", "sitekey")
                safe_ctx = {
                    key: value for key, value in ctx.items()
                    if not any(term in key.lower() for term in excluded)
                    and isinstance(value, (str, int, float, bool, type(None)))
                }
                mark(f"provider.datanodes.{message}", event="observation", **safe_ctx)
            except Exception:
                pass

        # 1. Check Official API Key if provided in secrets or environment
        api_key = (secrets.get("api_key") or secrets.get("datanodes_api_key") or os.environ.get("MOON_DN_API_KEY") or os.environ.get("DATANODES_API_KEY") or "").strip()
        if api_key:
            try:
                api_url = f"https://datanodes.to/api/file/direct_link?file_code={file_code}&key={api_key}"
                api_req = urllib.request.Request(api_url, headers=_headers(secrets))
                with route_http.urlopen(api_req, timeout=12) as api_resp:
                    payload = json.loads(api_resp.read().decode("utf-8", "replace"))
                    if isinstance(payload, dict) and payload.get("status") == 200:
                        direct = (payload.get("result") or {}).get("url")
                        size = (payload.get("result") or {}).get("size")
                        if direct:
                            from ..timer_scheduler import timer_scheduler
                            timer_scheduler.skip("datanodes.to", file_code, "api_key", telemetry_ctx={"task_id": task_id} if task_id else {})
                            return [cls._item(url, file_code, file_name, direct, size=size, metadata={"type": "file"})]
            except Exception:
                pass

        # 1b. Check if direct_url already acquired in secrets (e.g. via solver or UI)
        if secrets.get("direct_url"):
            from ..timer_scheduler import timer_scheduler
            timer_scheduler.skip("datanodes.to", file_code, "direct_url", telemetry_ctx={"task_id": task_id} if task_id else {})
            res_item = cls._item(url, file_code, file_name, secrets["direct_url"], metadata={"type": "file"})
            if isinstance(secrets.get("cookies"), dict):
                res_item.cookies = secrets["cookies"]
            return [res_item]

        # 2. Fast dead-link probe (curl_cffi Chrome impersonation with urllib fallback)
        t_start = time.time()
        probe_cookies: dict[str, str] = {}
        text = ""
        try:
            from .. import http_client
            from ..critical_trace import span
            with span("provider.datanodes.landing_get", resource="http", resource_id="datanodes.to"):
                r = http_client.get(url, headers=_headers(secrets), timeout=15)
            if r.status == 404:
                raise ProviderMappedError("DataNodes: File not found (HTTP 404)", "not_found", 404)
            text = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
            set_cookie = r.headers.get("set-cookie", "") if r.headers else ""
            cookie_match = re.findall(r"(?:^|[,; ])\s*([A-Za-z0-9_-]+)=([^;]+)", set_cookie)
            probe_cookies = {key: value for key, value in cookie_match}
            if r.status >= 400:
                logger.warning("DataNodes probe returned HTTP %s for %s", r.status, file_code)
        except ProviderMappedError:
            raise
        except Exception:
            try:
                req = urllib.request.Request(url, headers={
                    **_headers(secrets),
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                })
                with route_http.urlopen(req, timeout=15) as resp:
                    raw_body = resp.read(2 * 1024 * 1024)
                    text = raw_body.decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    err_text = exc.read(65536).decode("utf-8", "replace")
                    if OFFLINE_PATTERN.search(err_text):
                        raise ProviderMappedError("DataNodes: File was deleted or expired by owner/administration", "not_found", 404) from exc
                    raise ProviderMappedError("DataNodes: File not found (HTTP 404)", "not_found", 404) from exc
            except Exception:
                text = ""

        if text:
            assert_file_online(text, "DataNodes")

        # Extract estimated or declared file size from HTML if present
        discovered_size: Optional[int] = None
        if text:
            m_size = re.search(r'data-scan-size=["\']([^"\']+)["\']', text) or re.search(r'\(([\d.]+\s*[KMGT]?B)\)', text)
            if m_size:
                size_str = m_size.group(1).strip()
                sm = re.search(r'([\d.]+)\s*([KMGT]?B|bytes)', size_str, re.IGNORECASE)
                if sm:
                    val = float(sm.group(1))
                    unit = sm.group(2).upper()
                    mult = {"B": 1, "BYTES": 1, "KB": 1024, "MB": 1024 * 1024, "GB": 1024 * 1024 * 1024, "TB": 1024 * 1024 * 1024 * 1024}.get(unit, 1)
                    discovered_size = int(val * mult)

        def _probe_direct_size(direct_candidate: str) -> Optional[int]:
            if not direct_candidate:
                return discovered_size
            try:
                from .. import http_client
                h_resp = http_client.head(direct_candidate, timeout=5)
                cl = h_resp.headers.get("content-length") or h_resp.headers.get("Content-Length")
                if cl and str(cl).isdigit():
                    return int(cl)
            except Exception:
                pass
            return discovered_size

        # 3. Advance from Step 1 landing page to Step 2 (<download-countdown>) if necessary
        # DataNodes uses a 2-step flow: landing page has op=download1 and button #method_free
        is_step1 = ('name="op" value="download1"' in text or 'id="method_free"' in text or 'name="method_free"' in text)
        if is_step1:
            form_fname = cls._select_step1_fname(text, file_name)
            _trace("step1_fname", candidates=cls._fname_candidates(text), chosen=form_fname)
            m_rand1 = re.search(r'name=["\']rand["\']\s+value=["\']([^"\']+)["\']', text)
            form_rand = m_rand1.group(1) if m_rand1 else ""

            step1_data = {
                "op": "download1",
                "usr_login": "",
                "id": file_code,
                "fname": form_fname,
                "referer": "",
                "method_free": "Free Download >>",
            }
            if form_rand:
                step1_data["rand"] = form_rand

            cookie_parts_1 = [f"lang=english; file_name={form_fname}; file_code={file_code};"]
            for ck, cv in probe_cookies.items():
                if ck.lower() not in {"lang", "file_name", "file_code"}:
                    cookie_parts_1.append(f"{ck}={cv};")
            if secrets.get("cf_clearance"):
                cookie_parts_1.append(f"cf_clearance={secrets['cf_clearance']};")
            if isinstance(secrets.get("cookies"), dict):
                for ck, cv in secrets["cookies"].items():
                    if ck.lower() not in {"lang", "file_name", "file_code", "cf_clearance"}:
                        cookie_parts_1.append(f"{ck}={cv};")

            step1_headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": " ".join(cookie_parts_1),
                "Origin": "https://datanodes.to",
                "Referer": url,
                "Host": "datanodes.to",
                "User-Agent": str(secrets.get("user_agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            }
            try:
                from .. import http_client
                from ..critical_trace import span
                with span("provider.datanodes.step1_post", resource="http", resource_id="datanodes.to"):
                    r_step1 = http_client.post("https://datanodes.to/download", data=step1_data, headers=step1_headers, timeout=15)
                step2_html = r_step1.read(2 * 1024 * 1024).decode("utf-8", "replace")
                set_cookie_2 = r_step1.headers.get("set-cookie", "") if r_step1.headers else ""
                for k, v in re.findall(r"(?:^|[,; ])\s*([A-Za-z0-9_-]+)=([^;]+)", set_cookie_2):
                    probe_cookies[k] = v
                if step2_html and ("download-countdown" in step2_html or "challenges.cloudflare.com" in step2_html or "dl-token" in step2_html):
                    text = step2_html
            except Exception as exc:
                logger.debug("DataNodes Step 1 advance error: %s", exc)

        # 4. Extract Vue download component parameters from Step 2
        m_rand = re.search(r'rand=["\']([^"\']+)["\']', text)
        rand_val = m_rand.group(1) if m_rand else ""
        m_dl_token = re.search(r'dl-token=["\']([^"\']+)["\']', text)
        dl_token_val = m_dl_token.group(1) if m_dl_token else ""
        from ..timer_detector import TimerDetector
        timer_candidates = TimerDetector.detect_candidates(text)
        timer_observed_at = time.monotonic()
        best_timer = TimerDetector.best_candidate(timer_candidates)
        countdown_sec = best_timer.seconds if best_timer is not None else 10
        m_key = re.search(r'data-sitekey=["\']([^"\']+)["\']', text) or re.search(r'sitekey:\s*["\']([^"\']+)["\']', text)
        site_key = m_key.group(1) if m_key else "0x4AAAAAAD8U9nktqncPIkBM"
        needs_turnstile = is_step1 or ("challenges.cloudflare.com" in text or "cf-turnstile" in text or "data-sitekey" in text or "method_free" in text)

        harvested_cookies: dict[str, str] = {}
        freshly_solved = False
        solver_direct_url: Optional[str] = None
        fresh_timer_info: dict[str, Any] = {}

        from ..browser_solver import solver_daemon
        if task_id:
            if best_timer is not None and best_timer.seconds:
                solver_daemon._notify_stage(task_id, "countdown", {"countdown_seconds": countdown_sec})
            elif needs_turnstile:
                solver_daemon._notify_stage(task_id, "turnstile_detected", {})

        def _do_solve() -> tuple[str, dict[str, str]]:
            nonlocal freshly_solved, solver_direct_url, fresh_timer_info
            from ..telemetry import telemetry_bus
            if task_id:
                solver_daemon._notify_stage(task_id, "turnstile_detected", {})
            telemetry_bus.record(
                level="INFO", subsystem="engine:captcha",
                message=f"{pfx}[CAPTCHA_DETECTED] Cloudflare Turnstile detected on datanodes.to — solving in background with automated browser...",
                context={**t_ctx, "file_code": file_code, "url": url, "sitekey": site_key},
                tier="engine",
            )
            initial_cookies = {"lang": "english", "file_code": file_code}
            initial_cookies.update(probe_cookies)
            if isinstance(secrets.get("cookies"), dict):
                for ck, cv in secrets["cookies"].items():
                    if ck.lower() not in {"file_code", "file_name", "download_link", "task_id"}:
                        initial_cookies[ck] = cv
            solve_res = solver_daemon.solve_challenge_sync(url, timeout_seconds=50.0, initial_cookies=initial_cookies, task_id=task_id)
            if solve_res.get("success"):
                freshly_solved = True
                fresh_timer_info = dict(solve_res.get("timer") or {})
                if task_id:
                    solver_daemon._notify_stage(task_id, "turnstile_solved", {})
                tok = str(solve_res.get("turnstile_token") or "")
                cks = solve_res.get("cookies", {})
                if solve_res.get("direct_url"):
                    solver_direct_url = solve_res.get("direct_url")
                dur_s = (solve_res.get("duration_ms") or 0) / 1000.0
                telemetry_bus.record(
                    level="INFO", subsystem="engine:captcha",
                    message=f"{pfx}[CAPTCHA_BYPASSED] Challenge solved in {dur_s:.1f}s via {solve_res.get('engine')} — {'direct URL captured' if solver_direct_url else 'token acquired'}",
                    context={**t_ctx, "engine": solve_res.get("engine"), "duration_ms": solve_res.get("duration_ms"), "has_direct_url": bool(solver_direct_url)},
                    tier="engine",
                )
                return tok, cks
            return "", {}

        turnstile_tok = secrets.get("turnstile_token") or secrets.get("cf-turnstile-response") or ""

        # If solver is active from a background task, wait for the token
        is_bg_active = secrets.get("user_challenge", {}).get("solver_active", False)
        _trace("decision", has_token=bool(turnstile_tok), is_bg_active=bool(is_bg_active),
               needs_turnstile=bool(needs_turnstile), is_step1=bool(is_step1),
               has_timer=bool(best_timer is not None), timer_candidates=len(timer_candidates),
               countdown_sec=countdown_sec,
               has_rand=bool(rand_val), has_dl_token=bool(dl_token_val),
               cf_clearance=bool(secrets.get("cf_clearance")))

        # If Turnstile is required and no token yet, solve headlessly. Multipart
        # package members defer to the engine's package challenge coordinator so
        # the package solves once instead of one browser solve per part; a host
        # whose fresh solves keep getting rejected enters a bounded solve backoff.
        multipart_member = bool(secrets.get("multipart_package"))
        solve_backoff = _solve_backoff_active("datanodes.to")
        if needs_turnstile and not turnstile_tok:
            if is_bg_active or multipart_member or solve_backoff:
                if solve_backoff:
                    from ..telemetry import telemetry_bus
                    telemetry_bus.record(
                        level="WARN", subsystem="engine:captcha",
                        message=f"{pfx}[SOLVER_BACKOFF] DataNodes solve budget exhausted after repeated rejections; deferring",
                        context={**t_ctx, "file_code": file_code, "multipart": multipart_member},
                        tier="engine",
                    )
            else:
                try:
                    turnstile_tok, harvested_cookies = _do_solve()
                except Exception as e:
                    logger.debug("Automated solver on DataNodes failed: %s", e)

        # If solver captured direct link directly, return it immediately
        if solver_direct_url:
            actual_url = urllib.parse.unquote(solver_direct_url)
            final_size = _probe_direct_size(actual_url)
            res_item = cls._item(url, file_code, file_name, actual_url, size=final_size, metadata={"type": "file"})
            if harvested_cookies:
                res_item.cookies = harvested_cookies
            return [res_item]

        # If still missing, request user intervention with accurate metadata
        if needs_turnstile and not turnstile_tok:
            challenge_meta = {"url": url, "host": "datanodes.to", "page_url": url, "site_key": site_key, "cookies": {"file_code": file_code}}
            from .datanodes_resume import CONTINUATION_KEY, build_continuation
            challenge_meta[CONTINUATION_KEY] = build_continuation(
                url=url,
                file_code=file_code,
                file_name=file_name,
                rand=rand_val,
                dl_token=dl_token_val,
                cookies=probe_cookies,
                countdown_seconds=float(countdown_sec),
                site_key=site_key,
                discovered_size=discovered_size,
            )
            if is_bg_active:
                challenge_meta["solver_active"] = True
                challenge_meta["solver_engine"] = secrets.get("user_challenge", {}).get("solver_engine", "Clearcote")
                
            raise NeedsUser(
                "DataNodes requires Cloudflare Turnstile verification",
                "turnstile",
                challenge_meta,
            )

        # Ensure mandatory server-side countdown has elapsed before requesting download link.
        # Deadlines are owned by the cross-part TimerScheduler so multipart packages run
        # their waits down concurrently and only the final POST is serialized per host.
        from ..timer_scheduler import timer_scheduler
        host_label = "datanodes.to"
        part_key = file_code
        timer_ctx = {**t_ctx, "host": host_label, "file_code": file_code}
        satisfied_reason = None
        if freshly_solved and fresh_timer_info.get('detected') and int(fresh_timer_info.get('remaining') or 0) <= 0:
            satisfied_reason = "fresh_solve_countdown_elapsed"
        timer_scheduler.note_candidates(host_label, part_key, timer_candidates, telemetry_ctx=timer_ctx)
        if satisfied_reason:
            timer_scheduler.skip(host_label, part_key, satisfied_reason, telemetry_ctx=timer_ctx)
        else:
            elapsed = time.monotonic() - timer_observed_at
            remain = max(0.0, float(countdown_sec) - elapsed)
            if remain > 0:
                wait_secs = remain
                timer_scheduler.arm(
                    host_label, part_key, wait_secs,
                    source=(best_timer.source if best_timer is not None else "default"),
                    confidence=(best_timer.confidence if best_timer is not None else 0.2),
                    telemetry_ctx=timer_ctx, reason="pre_post_wait",
                )
                _trace("countdown_wait", seconds=wait_secs, parsed_seconds=countdown_sec,
                       elapsed=round(elapsed, 2),
                       source=(best_timer.source if best_timer is not None else "default"))
                timer_scheduler.wait_for_sync(host_label, part_key, telemetry_ctx=timer_ctx)
            else:
                timer_scheduler.satisfied(host_label, part_key, "already_elapsed", telemetry_ctx=timer_ctx)

        post_observation: dict[str, Any] = {"status": None, "final_url": None, "error": None}

        def _send_post(curr_tok: str, curr_cookies: dict[str, str]) -> str:
            from ..provider_wait import check_control
            check_control()
            post_data = {
                "op": "download2",
                "id": file_code,
                "rand": rand_val,
                "referer": "",
                "method_free": "",
                "method_premium": "",
                "g_captch__a": "1",
            }
            if dl_token_val:
                post_data["dl_token"] = dl_token_val
            if curr_tok:
                post_data["cf-turnstile-response"] = curr_tok

            cookie_parts = [f"lang=english; file_name={file_name}; file_code={file_code};"]
            if secrets.get("cf_clearance"):
                cookie_parts.append(f"cf_clearance={secrets['cf_clearance']};")
            if curr_cookies.get("cf_clearance"):
                cookie_parts.append(f"cf_clearance={curr_cookies['cf_clearance']};")
            if secrets.get("cookie"):
                cookie_parts.append(f"{secrets['cookie']};")
            if isinstance(secrets.get("cookies"), dict):
                for ck_k, ck_v in secrets["cookies"].items():
                    if ck_k.lower() not in ("lang", "file_name", "file_code", "cf_clearance"):
                        cookie_parts.append(f"{ck_k}={ck_v};")
            for ck_k, ck_v in curr_cookies.items():
                if ck_k.lower() not in ("lang", "file_name", "file_code", "cf_clearance"):
                    cookie_parts.append(f"{ck_k}={ck_v};")

            post_headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": " ".join(cookie_parts),
                "Host": "datanodes.to",
                "Origin": "https://datanodes.to",
                "Referer": "https://datanodes.to/download",
                "X-Dn-Dl": "1",
                "User-Agent": str(secrets.get("user_agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            }

            resp_text = ""
            try:
                from .. import http_client
                from ..critical_trace import span as trace_span
                with trace_span("provider.datanodes.step2_request", resource="http", resource_id="datanodes.to"):
                    r_post = http_client.post(
                        "https://datanodes.to/download",
                        data=post_data,
                        headers=post_headers,
                        timeout=15,
                    )
                post_observation["status"] = int(r_post.status)
                post_observation["final_url"] = r_post.geturl()
                with trace_span("provider.datanodes.step2_response", resource="http_body",
                                resource_id="datanodes.to", status=int(r_post.status)):
                    resp_text = r_post.read(1024 * 1024).decode("utf-8", "replace")
            except Exception as exc:
                post_observation["error"] = f"{type(exc).__name__}: {exc}"

            if not resp_text:
                try:
                    req_post = urllib.request.Request(
                        "https://datanodes.to/download",
                        data=urllib.parse.urlencode(post_data).encode("utf-8"),
                        headers=post_headers,
                        method="POST",
                    )
                    with route_http.urlopen(req_post, timeout=15) as post_resp:
                        post_observation["status"] = int(getattr(post_resp, "status", 200))
                        post_observation["final_url"] = post_resp.geturl()
                        resp_text = post_resp.read(1024 * 1024).decode("utf-8", "replace")
                except urllib.error.HTTPError as exc:
                    post_observation["status"] = int(exc.code)
                    post_observation["final_url"] = exc.geturl()
                    post_observation["error"] = f"HTTPError: {exc.code}"
                    try:
                        resp_text = exc.read(1024 * 1024).decode("utf-8", "replace")
                    except Exception:
                        resp_text = ""
                except Exception as exc:
                    post_observation["error"] = f"{type(exc).__name__}: {exc}"
            return resp_text

        def _extract_direct_url(resp_text: str) -> str | None:
            if not resp_text:
                return None
            if resp_text.strip().startswith("{") and resp_text.strip().endswith("}"):
                try:
                    data = json.loads(resp_text)
                    direct_url = data.get("url")
                    if direct_url:
                        return urllib.parse.unquote(direct_url)
                except Exception:
                    pass
            m_direct = re.search(r'https?://[a-zA-Z0-9_\-\.]+\.datanodes\.to/[^\s"\'<>]+', resp_text) or re.search(r'https?://[a-zA-Z0-9_\-\.]+\.dlproxy\.uk/[^\s"\'<>]+', resp_text)
            if m_direct:
                return m_direct.group(0)
            return None

        from ..critical_trace import mark, span
        mark("provider.datanodes.final_post.queued", event="state",
             resource="host_post", resource_id=host_label)
        with timer_scheduler.host_gate(host_label):
            with span("provider.datanodes.final_post", resource="host_post", resource_id=host_label):
                resp_str = _send_post(turnstile_tok, harvested_cookies)
        found_url = _extract_direct_url(resp_str)
        mark("provider.datanodes.direct_link_capture", event="state",
             resource="provider", resource_id=host_label,
             status=post_observation.get("status"), captured=bool(found_url),
             body_bytes=len((resp_str or "").encode("utf-8")))
        _trace("post_result", status=post_observation.get("status"),
               final_url=post_observation.get("final_url"),
               transport_error=post_observation.get("error"),
               found_direct=bool(found_url), body_len=len(resp_str or ""),
               body_snippet=(resp_str or "")[:180],
               body_has_challenge=any(m in (resp_str or "").lower() for m in (
                   "cf-turnstile", "challenges.cloudflare.com", "bm-site-verification",
                   "challenge-platform", "verify you are human")),
               level="WARN" if not found_url else "INFO")

        # If direct link not found and we haven't freshly solved yet, retry ONCE with fresh solve
        if not found_url and not freshly_solved and needs_turnstile and not _solve_backoff_active("datanodes.to"):
            try:
                from ..telemetry import telemetry_bus
                telemetry_bus.record(
                    level="WARN", subsystem="engine:captcha",
                    message=f"{pfx}[CAPTCHA_RETRY] DataNodes rejected token from secrets — attempting fresh background solve...",
                    context={**t_ctx, "file_code": file_code},
                    tier="engine",
                )
                fresh_tok, fresh_cookies = _do_solve()
                if solver_direct_url:
                    actual_url = urllib.parse.unquote(solver_direct_url)
                    final_size = _probe_direct_size(actual_url)
                    res_item = cls._item(url, file_code, file_name, actual_url, size=final_size, metadata={"type": "file"})
                    if harvested_cookies:
                        res_item.cookies = harvested_cookies
                    return [res_item]
                if fresh_tok:
                    turnstile_tok = fresh_tok
                    harvested_cookies = fresh_cookies
                    # The fresh solve re-navigated the step-2 flow, which restarts the
                    # server-side wait: re-arm the scheduler and honor the new countdown
                    # before retrying the final POST.
                    retry_detected = bool(fresh_timer_info.get("detected"))
                    retry_remaining = int(fresh_timer_info.get("remaining") or 0) if retry_detected else 0
                    if retry_detected and retry_remaining <= 0:
                        timer_scheduler.satisfied(
                            host_label, part_key, "fresh_solve_countdown_elapsed", telemetry_ctx=timer_ctx,
                        )
                    else:
                        if not retry_detected and best_timer is not None:
                            retry_remaining = int(best_timer.seconds)
                        if retry_remaining > 0:
                            timer_scheduler.rearm(
                                host_label, part_key,
                                seconds=retry_remaining + 1,
                                reason="fresh_solve_retry",
                                telemetry_ctx={**timer_ctx, "timer_source": str(fresh_timer_info.get("source") or "fresh_solve")},
                            )
                            timer_scheduler.wait_for_sync(host_label, part_key, telemetry_ctx=timer_ctx)
                        else:
                            timer_scheduler.skip(
                                host_label, part_key, "no_timer_after_fresh_solve", telemetry_ctx=timer_ctx,
                            )
                    with timer_scheduler.host_gate(host_label):
                        resp_str = _send_post(turnstile_tok, harvested_cookies)
                    found_url = _extract_direct_url(resp_str)
                    mark("provider.datanodes.direct_link_capture", event="state",
                         resource="provider", resource_id=host_label,
                         status=post_observation.get("status"), captured=bool(found_url),
                         body_bytes=len((resp_str or "").encode("utf-8")), retry=True)
            except Exception as e:
                logger.debug("Retry solve on DataNodes failed: %s", e)

        if found_url:
            actual_url = urllib.parse.unquote(found_url)
            final_size = _probe_direct_size(actual_url)
            res_item = cls._item(url, file_code, file_name, actual_url, size=final_size, metadata={"type": "file"})
            if harvested_cookies:
                res_item.cookies = harvested_cookies
            return [res_item]

        if resp_str:
            from ..telemetry import telemetry_bus
            response_lower = resp_str.lower()
            challenge_markers = (
                "bm-site-verification",
                "challenges.cloudflare.com",
                "challenge-platform",
                "cf-turnstile",
                "verify you are human",
                "checking your browser",
            )
            telemetry_bus.record(
                level="WARN", subsystem="engine:resolve",
                message=f"{pfx}[PROVIDER_REJECTED] DataNodes POST did not yield download link: {resp_str[:160]}",
                context={
                    **t_ctx,
                    "file_code": file_code,
                    "resp_snippet": resp_str[:400],
                    "http_status": post_observation.get("status"),
                    "final_url": post_observation.get("final_url"),
                    "transport_error": post_observation.get("error"),
                },
                tier="engine",
            )
            resp_clean = re.sub(r'(?is)<script[^>]*>.*?</script>', '', resp_str)
            is_rate_limit = (
                post_observation.get("status") in {429, 509} or
                bool(re.search(r'\b(?:please\s+wait|wait\s+\d+\s*(?:sec|min|second|minute)|download\s+limit|limit\s+reached|parallel\s+download|bandwidth\s+limit)\b', resp_clean, re.I))
            )
            if is_rate_limit:
                rate_candidates = TimerDetector.detect_candidates(resp_clean)
                rate_best = TimerDetector.best_candidate(rate_candidates)
                timer_scheduler.note_rate_limited(
                    host_label, part_key,
                    seconds=(rate_best.seconds if rate_best is not None else None),
                    status=int(post_observation.get("status") or 429),
                    telemetry_ctx=timer_ctx,
                )
                raise ProviderMappedError("DataNodes: Download limit or wait time enforced by host", "rate_limited", int(post_observation.get("status") or 429))
            if any(marker in response_lower for marker in challenge_markers):
                if freshly_solved:
                    rejection_count = _note_solve_rejection("datanodes.to")
                    from ..telemetry import telemetry_bus as _tb
                    _tb.record(
                        level="WARN", subsystem="engine:captcha",
                        message=f"{pfx}[SOLVER_REJECTED] DataNodes rejected a freshly solved token "
                                f"({rejection_count} in window)",
                        context={**t_ctx, "file_code": file_code, "rejections_in_window": rejection_count,
                                 "http_status": post_observation.get("status")},
                        tier="engine",
                    )
                raise NeedsUser(
                    "DataNodes rejected the download request with a browser challenge",
                    "turnstile",
                    {
                        "url": url,
                        "host": "datanodes.to",
                        "page_url": url,
                        "site_key": site_key,
                        "response_status": post_observation.get("status"),
                        "response_reason": "provider_challenge_after_token",
                    },
                )

        if post_observation.get("status") in {403, 503}:
            raise ProviderMappedError(
                f"DataNodes: provider rejected download request (HTTP {post_observation['status']})",
                "retryable", int(post_observation["status"]),
            )

        detail = post_observation.get("error") or f"HTTP {post_observation.get('status') or 'unknown'} with empty response"
        raise ProviderUnavailable(f"DataNodes download link not found ({detail})")


# ---------------------------------------------------------------------------

# 40. Webmshare (webmshare.com)
# ---------------------------------------------------------------------------
class WebmshareProvider(HostedProvider):
    id, hosts = "webmshare", ("webmshare.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'<source[^>]+src=["\']([^"\']+)["\']', text) or \
            re.search(r'property=["\']og:video["\'][^>]*content=["\']([^"\']+)["\']', text)
        if m:
            direct = urllib.parse.urljoin(final, html.unescape(m.group(1)))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Webmshare video not found")


# ---------------------------------------------------------------------------
# 41. SendNow (send.now)
# ---------------------------------------------------------------------------
class SendNowProvider(HostedProvider):
    id, hosts = "send_now", ("send.now",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        dl = re.search(r'href=["\']([^"\']*(?:download|file)[^"\']+)["\']', text, re.I)
        if dl:
            direct = urllib.parse.urljoin(final, html.unescape(dl.group(1)))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("SendNow download link not found")


# ---------------------------------------------------------------------------
# 42. Giphy (giphy.com)
# ---------------------------------------------------------------------------
class GiphyProvider(HostedProvider):
    id, hosts = "giphy", ("giphy.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'property=["\']og:video["\'][^>]*content=["\']([^"\']+)["\']', text) or \
            re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Giphy media not found")


# ---------------------------------------------------------------------------
# 43. Clyp.it (clyp.it)
# ---------------------------------------------------------------------------
class ClypItProvider(HostedProvider):
    id, hosts = "clyp_it", ("clyp.it",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        cid = [p for p in parsed.path.split("/") if p][-1]
        data = _json(f"https://api.clyp.it/{cid}", secrets)
        direct = data.get("Mp3Url") or data.get("SecureMp3Url")
        if direct:
            fname = data.get("Title") or f"{cid}.mp3"
            if not fname.endswith(".mp3"):
                fname += ".mp3"
            return [cls._item(url, cid, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Clyp audio not found")


# ---------------------------------------------------------------------------
# 44. Bandcamp (bandcamp.com)
# ---------------------------------------------------------------------------
class BandcampProvider(HostedProvider):
    id, hosts = "bandcamp", ("bandcamp.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'data-tralbum=["\']([^"\']+)["\']', text)
        if m:
            tralbum = json.loads(html.unescape(m.group(1)))
            trackinfo = tralbum.get("trackinfo", [{}])[0]
            fileinfo = trackinfo.get("file", {})
            direct = fileinfo.get("mp3-128") or next(iter(fileinfo.values()), None)
            if direct:
                fname = trackinfo.get("title") or "track.mp3"
                return [cls._item(url, str(trackinfo.get("id", "track")), fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Bandcamp track not found")


# ---------------------------------------------------------------------------
# 45. Vipr (vipr.im)
# ---------------------------------------------------------------------------
class ViprProvider(HostedProvider):
    id, hosts = "vipr", ("vipr.im",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'<source[^>]+src=["\']([^"\']+)["\']', text) or \
            re.search(r'property=["\']og:video["\'][^>]*content=["\']([^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Vipr video not found")


# ---------------------------------------------------------------------------
# 47. WeTransfer (wetransfer.com, we.tl)
# ---------------------------------------------------------------------------
class WeTransferProvider(HostedProvider):
    id, hosts = "wetransfer", ("wetransfer.com", "we.tl")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        # Handle short link redirect if we.tl
        if "we.tl" in (parsed.hostname or ""):
            _, final, _ = _fetch(url, secrets)
            url = final
            parsed = urllib.parse.urlsplit(url)

        parts = [p for p in parsed.path.split("/") if p]
        # downloads/<file_id>/<security_hash> or downloads/<file_id>/<recipient_id>/<security_hash>
        if len(parts) >= 3 and parts[0] == "downloads":
            file_id = parts[1]
            security_hash = parts[-1]
            recipient_id = parts[2] if len(parts) >= 4 else None
            payload: dict[str, Any] = {"intent": "entire_transfer", "security_hash": security_hash}
            if recipient_id:
                payload["recipient_id"] = recipient_id
            api_url = f"https://wetransfer.com/api/v4/transfers/{file_id}/download"
            resp = _post_json(api_url, json_body=payload, secrets=secrets)
            direct = resp.get("direct_link")
            if direct:
                fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1] or f"wetransfer-{file_id}.zip"
                return [cls._item(url, file_id, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("WeTransfer direct download link could not be generated")


# ---------------------------------------------------------------------------
# 48. Cloud.mail.ru (cloud.mail.ru)
# ---------------------------------------------------------------------------
class CloudMailRuProvider(HostedProvider):
    id, hosts = "cloud_mail_ru", ("cloud.mail.ru",)

    @classmethod
    def _dispatcher(cls, secrets: dict[str, Any] | None = None) -> str:
        data = _json("https://cloud.mail.ru/api/v3/dispatcher", secrets)
        return data["body"]["weblink_get"][0]["url"].rstrip("/")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        parts = [p for p in parsed.path.split("/") if p]
        weblink = "/".join(parts[1:]) if len(parts) > 1 and parts[0] == "public" else "/".join(parts)
        dispatcher = cls._dispatcher(secrets)
        info_url = f"https://cloud.mail.ru/api/v4/public/list?weblink={urllib.parse.quote(weblink)}&sort=name&order=asc&offset=0&limit=500&version=4"
        data = _json(info_url, secrets)
        if data.get("type") == "file":
            fname = data.get("name") or "mailru_file"
            direct = f"{dispatcher}/{weblink.lstrip('/')}"
            return [cls._item(url, weblink, fname, direct, data.get("size"), metadata={"type": "file"})]
        return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        parts = [p for p in parsed.path.split("/") if p]
        weblink = "/".join(parts[1:]) if len(parts) > 1 and parts[0] == "public" else "/".join(parts)
        dispatcher = cls._dispatcher(secrets)
        items: list[ResolvedItem] = []

        def walk(current_path: str, prefix: str = ""):
            info_url = f"https://cloud.mail.ru/api/v4/public/list?weblink={urllib.parse.quote(current_path)}&sort=name&order=asc&offset=0&limit=500&version=4"
            data = _json(info_url, secrets)
            for node in data.get("list", []):
                name = node.get("name") or "item"
                node_weblink = node.get("weblink", "")
                rel = f"{prefix}/{name}".strip("/")
                if node.get("type") == "folder":
                    items.append(cls._item(url, f"folder:{node_weblink}", name, None, metadata={"type": "folder", "mode": "tree"}))
                    walk(node_weblink, rel)
                else:
                    direct = f"{dispatcher}/{node_weblink.lstrip('/')}"
                    items.append(cls._item(url, node_weblink, name, direct, node.get("size"), relative=rel, metadata={"type": "file", "mode": "tree"}))

        try:
            walk(weblink)
        except Exception:
            pass
        return items or cls.resolve(url, secrets)


# ---------------------------------------------------------------------------
# 49. Yandex.Disk (disk.yandex.com, disk.yandex.ru, disk.yandex.com.tr, yadi.sk)
# ---------------------------------------------------------------------------
class YandexDiskProvider(HostedProvider):
    id = "yandex_disk"
    hosts = ("disk.yandex.com", "disk.yandex.ru", "disk.yandex.com.tr", "yadi.sk")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        dl_api = f"https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key={urllib.parse.quote(url)}"
        try:
            res = _json(dl_api, secrets)
            direct = res.get("href")
            if direct:
                info_api = f"https://cloud-api.yandex.net/v1/disk/public/resources?public_key={urllib.parse.quote(url)}"
                info = _json(info_api, secrets)
                fname = info.get("name") or "yandex_file"
                return [cls._item(url, info.get("resource_id", url), fname, direct, info.get("size"), metadata={"type": "file"})]
        except Exception:
            pass
        return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        info_api = f"https://cloud-api.yandex.net/v1/disk/public/resources?public_key={urllib.parse.quote(url)}&limit=100"
        data = _json(info_api, secrets)
        items: list[ResolvedItem] = []
        embedded = data.get("_embedded", {})
        for node in embedded.get("items", []):
            name = node.get("name") or "item"
            is_dir = node.get("type") == "dir"
            item_url = node.get("public_url") or url
            if is_dir:
                items.append(cls._item(url, f"folder:{name}", name, None, metadata={"type": "folder", "mode": "tree"}))
            else:
                items.append(cls._item(url, node.get("resource_id", name), name, node.get("file"), node.get("size"), relative=name, metadata={"type": "file", "mode": "tree"}))
        return items or cls.resolve(url, secrets)


# ---------------------------------------------------------------------------
# 50. Rootz (rootz.so, www.rootz.so)
# ---------------------------------------------------------------------------
class RootzProvider(HostedProvider):
    id, hosts = "rootz", ("rootz.so", "www.rootz.so")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        file_id = [p for p in parsed.path.split("/") if p][-1]
        endpoint = "download-by-short" if "-" not in file_id else "download"
        api_url = f"https://www.rootz.so/api/files/{endpoint}/{file_id}"
        data = _json(api_url, secrets).get("data", {})
        direct = data.get("url")
        fname = data.get("filename") or data.get("fileName") or f"rootz-{file_id}"
        if direct:
            return [cls._item(url, file_id, fname, direct, data.get("size"), metadata={"type": "file"})]
        raise ProviderUnavailable("Rootz download link not found")


# ---------------------------------------------------------------------------
# 51. GUpload (gupload.xyz, gupload.to)
# ---------------------------------------------------------------------------
class GUploadProvider(HostedProvider):
    id, hosts = "gupload", ("gupload.xyz", "gupload.to")
    _XOR_KEY = b"G7#kP!2qZxV9mRwL"

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r"var\s+_cfg\s*=\s*['\"]([^'\"]+)['\"]", text)
        if m:
            raw_cfg = m.group(1)
            payload = raw_cfg.split("~", 1)[-1]
            import itertools
            decrypted_bytes = bytearray(b ^ k for b, k in zip(base64.b64decode(payload), itertools.cycle(cls._XOR_KEY)))
            cfg = json.loads(decrypted_bytes.decode("utf-8", "ignore"))
            video_url = cfg.get("videoUrl")
            if video_url:
                fname = urllib.parse.urlsplit(video_url).path.rsplit("/", 1)[-1] or "video.mp4"
                return [cls._item(url, urllib.parse.urlsplit(final).path, fname, video_url, metadata={"type": "file"})]
        raise ProviderUnavailable("GUpload stream link not found")


# ---------------------------------------------------------------------------
# 52. Archive.org (archive.org)
# ---------------------------------------------------------------------------
class ArchiveOrgProvider(HostedProvider):
    id, hosts = "archive_org", ("archive.org",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "download" and len(parts) >= 3:
            # Direct file download link: /download/<identifier>/<filename>
            fname = parts[-1]
            return [cls._item(url, parsed.path, fname, url, metadata={"type": "file"})]
        return [item for item in cls.enumerate(url, secrets) if (item.metadata or {}).get("type") == "file"]

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        parts = [p for p in parsed.path.split("/") if p]
        identifier = parts[1] if len(parts) >= 2 else (parts[0] if parts else "")
        meta = _json(f"https://archive.org/metadata/{identifier}", secrets)
        items: list[ResolvedItem] = []
        for f in meta.get("files", []):
            fname = f.get("name") or "file"
            size = int(f["size"]) if str(f.get("size", "")).isdigit() else None
            direct = f"https://archive.org/download/{identifier}/{fname.lstrip('/')}"
            items.append(cls._item(url, fname, fname, direct, size, relative=fname.lstrip("/"), metadata={"type": "file", "mode": "tree", "md5": f.get("md5")}))
        return items or cls.resolve(url, secrets)


# ---------------------------------------------------------------------------
# 53. Nova Storage (nova.storage)
# ---------------------------------------------------------------------------
class NovaStorageProvider(HostedProvider):
    id, hosts = "nova_storage", ("nova.storage",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        parts = [p for p in parsed.path.split("/") if p]
        file_id = parts[1] if len(parts) >= 2 and parts[0] == "d" else parts[-1]
        direct = f"https://nova.storage/api/file/{file_id}"
        try:
            info = _json(f"https://nova.storage/api/file/{file_id}/info", secrets)
            fname = info.get("name") or f"nova-{file_id}"
            size = int(info["size"]) if str(info.get("size", "")).isdigit() else None
            return [cls._item(url, file_id, fname, direct, size, metadata={"type": "file"})]
        except Exception:
            return [cls._item(url, file_id, f"nova-{file_id}", direct, metadata={"type": "file"})]


# ---------------------------------------------------------------------------
# 54. Flickr (flickr.com, www.flickr.com)
# ---------------------------------------------------------------------------
class FlickrProvider(HostedProvider):
    id, hosts = "flickr", ("flickr.com", "www.flickr.com")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', text) or \
            re.search(r'href=["\'](https?://live\.staticflickr\.com/[^"\']+)["\']', text)
        if m:
            direct = html.unescape(m.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Flickr photo link not found")


# ---------------------------------------------------------------------------
# 55. VSCO (vsco.co)
# ---------------------------------------------------------------------------
class VSCOProvider(HostedProvider):
    id, hosts = "vsco", ("vsco.co",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'window\.__PRELOADED_STATE__\s*=\s*({.+?});</script>', text)
        if m:
            try:
                state = json.loads(m.group(1))
                medias = state.get("medias", {}).get("byId", {})
                for item_dict in medias.values():
                    media = item_dict.get("media", {})
                    direct = media.get("videoUrl") or ("https://" + media["responsiveUrl"] if "responsiveUrl" in media else None)
                    if direct:
                        fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1] or "vsco_media"
                        return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
            except Exception:
                pass
        og = re.search(r'property=["\']og:(?:video|image)["\'][^>]*content=["\']([^"\']+)["\']', text)
        if og:
            direct = html.unescape(og.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("VSCO media not found")


# ---------------------------------------------------------------------------
# 56. Twitter / X (twitter.com, x.com, fxtwitter.com, vxtwitter.com)
# ---------------------------------------------------------------------------
class TwitterProvider(HostedProvider):
    id, hosts = "twitter", ("twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        parts = [p for p in parsed.path.split("/") if p]
        status_id = parts[2] if len(parts) >= 3 and parts[1] == "status" else parts[-1]
        data = _json(f"https://api.fxtwitter.com/2/status/{status_id}", secrets)
        tweet = data.get("tweet", {})
        media = tweet.get("media", {})
        items = []
        for vid in media.get("videos", []):
            direct = vid.get("url")
            if direct:
                items.append(cls._item(url, f"vid:{status_id}", f"{status_id}.mp4", direct, metadata={"type": "file"}))
        for photo in media.get("photos", []):
            direct = photo.get("url")
            if direct:
                fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1] or f"{status_id}.jpg"
                items.append(cls._item(url, f"photo:{fname}", fname, direct, metadata={"type": "file"}))
        if items:
            return items
        raise ProviderUnavailable("Twitter media not found in tweet")


# ---------------------------------------------------------------------------
# 57. TikTok (tiktok.com, www.tiktok.com, vm.tiktok.com, vt.tiktok.com)
# ---------------------------------------------------------------------------
class TikTokProvider(HostedProvider):
    id = "tiktok"
    hosts = ("tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        try:
            data = _json(f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}", secrets).get("data", {})
            direct = data.get("play") or data.get("wmplay")
            if direct:
                fname = f"tiktok_{data.get('id', 'video')}.mp4"
                return [cls._item(url, str(data.get("id", "video")), fname, direct, data.get("size"), metadata={"type": "file"})]
        except Exception:
            pass
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        og = re.search(r'property=["\']og:video["\'][^>]*content=["\']([^"\']+)["\']', text)
        if og:
            direct = html.unescape(og.group(1))
            return [cls._item(url, urllib.parse.urlsplit(final).path, "tiktok_video.mp4", direct, metadata={"type": "file"})]
        raise ProviderUnavailable("TikTok video not found")


# ---------------------------------------------------------------------------
# 58. Pinterest (pinterest.com, www.pinterest.com, pin.it)
# ---------------------------------------------------------------------------
class PinterestProvider(HostedProvider):
    id, hosts = "pinterest", ("pinterest.com", "www.pinterest.com", "pin.it")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        og = re.search(r'property=["\']og:(?:video|image)["\'][^>]*content=["\']([^"\']+)["\']', text)
        if og:
            direct = html.unescape(og.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Pinterest pin media not found")


# ---------------------------------------------------------------------------
# 59. Odysee (odysee.com)
# ---------------------------------------------------------------------------
class OdyseeProvider(HostedProvider):
    id, hosts = "odysee", ("odysee.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        og = re.search(r'property=["\']og:video["\'][^>]*content=["\']([^"\']+)["\']', text)
        if og:
            direct = html.unescape(og.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1] or "odysee_video.mp4"
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Odysee video stream not found")


# ---------------------------------------------------------------------------
# 60. Rumble (rumble.com)
# ---------------------------------------------------------------------------
class RumbleProvider(HostedProvider):
    id, hosts = "rumble", ("rumble.com",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        m = re.search(r'"embedUrl":\s*"https://rumble\.com/embed/([^/"]+)', text) or \
            re.search(r'/embedJS/u3\?.*?v=([^"&]+)', text) or \
            re.search(r'https://rumble\.com/embed/([a-zA-Z0-9]+)', text)
        embed_id = m.group(1) if m else None
        if embed_id:
            api_url = f"https://rumble.com/embedJS/u3?request=video&ver=2&v={embed_id}"
            data = _json(api_url, secrets)
            ua = data.get("ua", {})
            mp4_dict = ua.get("mp4", {})
            best = next(iter(reversed(list(mp4_dict.values()))), None)
            if best and isinstance(best, dict) and best.get("url"):
                fname = f"{data.get('title', 'rumble')}.mp4"
                return [cls._item(url, embed_id, fname, best["url"], metadata={"type": "file"})]
        og = re.search(r'property=["\']og:video["\'][^>]*content=["\']([^"\']+)["\']', text)
        if og:
            direct = html.unescape(og.group(1))
            return [cls._item(url, urllib.parse.urlsplit(final).path, "rumble_video.mp4", direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Rumble video link not found")


# ---------------------------------------------------------------------------
# 61. DailyMotion (dailymotion.com, dai.ly)
# ---------------------------------------------------------------------------
class DailyMotionProvider(HostedProvider):
    id, hosts = "dailymotion", ("dailymotion.com", "dai.ly")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        vid_id = [p for p in parsed.path.split("/") if p][-1]
        try:
            meta = _json(f"https://www.dailymotion.com/player/metadata/video/{vid_id}", secrets)
            qualities = meta.get("qualities", {})
            for q_list in qualities.values():
                for item in q_list:
                    if item.get("url"):
                        title = meta.get("title") or f"dailymotion_{vid_id}"
                        return [cls._item(url, vid_id, f"{title}.mp4", item["url"], metadata={"type": "file"})]
        except Exception:
            pass
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        og = re.search(r'property=["\']og:video["\'][^>]*content=["\']([^"\']+)["\']', text)
        if og:
            direct = html.unescape(og.group(1))
            return [cls._item(url, vid_id, f"dailymotion_{vid_id}.mp4", direct, metadata={"type": "file"})]
        raise ProviderUnavailable("DailyMotion video not found")


# ---------------------------------------------------------------------------
# 62. Twitch (twitch.tv, www.twitch.tv, clips.twitch.tv)
# ---------------------------------------------------------------------------
class TwitchProvider(HostedProvider):
    id, hosts = "twitch", ("twitch.tv", "www.twitch.tv", "clips.twitch.tv")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        body, final, _ = _fetch(url, secrets)
        text = body.decode("utf-8", "replace")
        og = re.search(r'property=["\']og:video["\'][^>]*content=["\']([^"\']+)["\']', text) or \
            re.search(r'<source[^>]+src=["\']([^"\']+)["\']', text)
        if og:
            direct = html.unescape(og.group(1))
            fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1] or "twitch_clip.mp4"
            return [cls._item(url, urllib.parse.urlsplit(final).path, fname, direct, metadata={"type": "file"})]
        raise ProviderUnavailable("Twitch clip or video not found")


# ---------------------------------------------------------------------------
# 63. Chevereto Generic (pattern crawler for independent Chevereto instances)
# ---------------------------------------------------------------------------
class CheveretoGenericProvider(CheveretoBaseProvider):
    id = "chevereto_generic"
    hosts = ("*chevereto*",)

    @classmethod
    def match_path(cls, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in ("/a/", "/album/", "/i/", "/img/", "/image/", "/video/", "/videos/"))


# ---------------------------------------------------------------------------
# 64. OwnCloud / Nextcloud (pattern crawler for self-hosted instances)
# ---------------------------------------------------------------------------
class OwnCloudProvider(HostedProvider):
    id = "owncloud"
    hosts = ("*owncloud*", "*nextcloud*")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parts = [p for p in parsed.path.split("/") if p]
        share_token = parts[-1] if parts else ""
        if len(parts) >= 2 and parts[-2] == "s":
            share_token = parts[-1]
        direct = f"{origin}/index.php/s/{share_token}/download"
        return [cls._item(url, share_token, f"owncloud_{share_token}", direct, metadata={"type": "file"})]

    @classmethod
    def enumerate(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        return cls.resolve(url, secrets)


# ---------------------------------------------------------------------------
# 65. Discourse (pattern crawler for Discourse forums)
# ---------------------------------------------------------------------------
class DiscourseProvider(HostedProvider):
    id = "discourse"
    hosts = ("*discourse*", "forums.plex.tv", "meta.discourse.org")

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        # Direct attachment in /uploads/
        if "/uploads/" in parsed.path:
            fname = parsed.path.rsplit("/", 1)[-1]
            return [cls._item(url, parsed.path, fname, url, metadata={"type": "file"})]
        parts = [p for p in parsed.path.split("/") if p]
        topic_id = None
        if len(parts) >= 2 and parts[0] == "t":
            topic_id = parts[2] if len(parts) >= 3 else parts[1]
        if not topic_id:
            raise ProviderUnavailable("Discourse topic ID could not be identified")
        data = _json(f"{origin}/t/{topic_id}.json", secrets)
        posts = data.get("post_stream", {}).get("posts", [])
        items = []
        for p in posts:
            cooked = p.get("cooked", "")
            for m in re.finditer(r'href=["\']([^"\']*(?:/uploads/|/original/)[^"\']+)["\']', cooked):
                direct = urllib.parse.urljoin(origin, html.unescape(m.group(1)))
                fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
                items.append(cls._item(url, direct, fname, direct, metadata={"type": "file"}))
        if items:
            return items
        raise ProviderUnavailable("No downloadable attachments found in Discourse topic")


# ---------------------------------------------------------------------------
# 66. WordPress Media (pattern crawler for WP instances)
# ---------------------------------------------------------------------------
class WordPressMediaProvider(HostedProvider):
    id = "wordpress_media"
    hosts = ("*wordpress*",)

    @classmethod
    def resolve(cls, url: str, secrets: dict[str, Any] | None = None) -> list[ResolvedItem]:
        parsed = urllib.parse.urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if "/wp-content/uploads/" in parsed.path:
            fname = parsed.path.rsplit("/", 1)[-1]
            return [cls._item(url, parsed.path, fname, url, metadata={"type": "file"})]
        try:
            data = _json(f"{origin}/wp-json/wp/v2/media?per_page=20", secrets)
            items = []
            for m in data:
                direct = m.get("source_url")
                if direct:
                    fname = urllib.parse.urlsplit(direct).path.rsplit("/", 1)[-1]
                    items.append(cls._item(url, str(m.get("id", fname)), fname, direct, metadata={"type": "file"}))
            if items:
                return items
        except Exception:
            pass
        raise ProviderUnavailable("WordPress media could not be resolved")


CYBERDROP_PROVIDERS = [
    VikingfileProvider, FilesterProvider, KoofrProvider, FileditchProvider,
    IceyfileProvider, CyberfileProvider, ImgLikeProvider, ImagePondProvider,
    ImgBBProvider, CatboxProvider, UploadEEProvider, BunkrProvider,
    AnonTransferProvider, PCloudProvider, OneDriveProvider, DropboxProvider,
    BoxProvider, PostImgProvider, ImgBoxProvider, ImageBamProvider,
    ImxToProvider, ImageVenueProvider, PixHostProvider, ImgurProvider,
    StreamableProvider,
    SendVidProvider, WhypItProvider,
    BuzzHeavierProvider, PillowCaseProvider, MixdropProvider, DoodstreamProvider,
    StreamtapeProvider, VoeProvider, FuckingFastProvider, DatanodesProvider, WebmshareProvider,
    SendNowProvider, GiphyProvider, ClypItProvider, BandcampProvider,
    ViprProvider, WeTransferProvider, CloudMailRuProvider,
    YandexDiskProvider, RootzProvider, GUploadProvider, ArchiveOrgProvider,
    NovaStorageProvider, FlickrProvider, VSCOProvider, TwitterProvider,
    TikTokProvider, PinterestProvider, OdyseeProvider, RumbleProvider,
    DailyMotionProvider, TwitchProvider, CheveretoGenericProvider,
    OwnCloudProvider, DiscourseProvider, WordPressMediaProvider,
    ]
