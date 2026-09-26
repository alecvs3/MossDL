from __future__ import annotations

import base64
import codecs
import html
import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Any, Callable

from .shortlinks import _safe_url
from . import route_http

logger = logging.getLogger(__name__)

_KNOWN_PARAM_KEYS = (
    "url", "dest", "destination", "target", "redirect", "r", "u", "link",
    "go", "to", "v", "href", "out", "file", "download", "token", "payload"
)


class DeepParamDecoder:
    """
    Tier 0: Offline high-speed parameter and encoding unpacker.
    Recovers destination URLs obscured behind query strings, base64, hex, rot13, or path prefixes.
    """

    @staticmethod
    def decode(url: str) -> str | None:
        clean_url = url.strip()
        parsed = urllib.parse.urlparse(clean_url)
        params = urllib.parse.parse_qs(parsed.query)

        # 1. Check embedded URL in query parameters
        for key in _KNOWN_PARAM_KEYS:
            if key in params:
                val = params[key][0].strip()
                unpacked = DeepParamDecoder._try_decode_token(val)
                if unpacked and unpacked != clean_url:
                    if parsed.fragment and "#" not in unpacked:
                        unpacked = f"{unpacked}#{parsed.fragment}"
                    if _safe_url(unpacked, clean_url):
                        return unpacked

        # 2. Check URL in path segments (e.g. /go/https://..., /link/https://...)
        path = parsed.path
        for prefix in ("/go/", "/link/", "/out/", "/jump/", "/external/", "/redirect/"):
            if prefix in path.lower():
                idx = path.lower().find(prefix) + len(prefix)
                candidate = path[idx:].strip()
                if parsed.fragment and "#" not in candidate:
                    candidate = f"{candidate}#{parsed.fragment}"
                if candidate.startswith(("http://", "https://")) and _safe_url(candidate, clean_url):
                    return candidate
                unpacked = DeepParamDecoder._try_decode_token(candidate)
                if unpacked and unpacked != clean_url:
                    if parsed.fragment and "#" not in unpacked:
                        unpacked = f"{unpacked}#{parsed.fragment}"
                    if _safe_url(unpacked, clean_url):
                        return unpacked

        return None

    @staticmethod
    def _try_decode_token(token: str) -> str | None:
        """Attempts multiple encoding unmasking strategies on a string token."""
        if not token:
            return None

        # A. Plain URL
        if token.startswith(("http://", "https://")):
            return token

        # B. Standard & URL-safe Base64
        try:
            padded = token + "=" * (-len(token) % 4)
            decoded = base64.b64decode(padded).decode("utf-8", errors="ignore").strip()
            if decoded.startswith(("http://", "https://")):
                return decoded
            if decoded.startswith("{") and decoded.endswith("}"):
                try:
                    obj = json.loads(decoded)
                    if isinstance(obj, dict):
                        for k in ("url", "dest", "destination", "target", "link", "download"):
                            if k in obj and isinstance(obj[k], str) and obj[k].startswith(("http://", "https://")):
                                return obj[k]
                except Exception:
                    pass
        except Exception:
            pass

        # C. Reversed Base64 (Common pattern in BASD and modern faucets)
        try:
            reversed_token = token[::-1]
            padded = reversed_token + "=" * (-len(reversed_token) % 4)
            decoded = base64.b64decode(padded).decode("utf-8", errors="ignore").strip()
            if decoded.startswith(("http://", "https://")):
                return decoded
            if decoded.startswith("{") and decoded.endswith("}"):
                try:
                    obj = json.loads(decoded)
                    if isinstance(obj, dict):
                        for k in ("url", "dest", "destination", "target", "link", "download"):
                            if k in obj and isinstance(obj[k], str) and obj[k].startswith(("http://", "https://")):
                                return obj[k]
                except Exception:
                    pass
        except Exception:
            pass

        # D. Hex-encoded ASCII
        try:
            if re.fullmatch(r"[0-9a-fA-F]+", token) and len(token) % 2 == 0 and len(token) > 14:
                decoded = bytes.fromhex(token).decode("utf-8", errors="ignore").strip()
                if decoded.startswith(("http://", "https://")):
                    return decoded
        except Exception:
            pass

        # E. ROT13 / Caesar cipher
        try:
            decoded = codecs.decode(token, "rot_13").strip()
            if decoded.startswith(("http://", "https://")):
                return decoded
        except Exception:
            pass

        return None


class SpecializedRuleRegistry:
    """
    Tier 1: Native Python ports of proven rules from FastForward and AdsBypasser.
    """

    def __init__(self, user_agent: str | None = None, request_timeout: float = 8.0) -> None:
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        self.request_timeout = request_timeout

    def match_and_bypass(self, url: str) -> str | None:
        """Synchronously checks domain patterns that can be transformed without network roundtrips."""
        clean = url.strip()

        # 1. Network-Loop to AdShrink transform
        m_shrink = re.search(r'shrink-service\.it/btn/([A-Za-z0-9_-]+)', clean)
        if m_shrink:
            return f"https://adshnk.com/{m_shrink.group(1)}"

        # 2. Ouo / Ouo.press fast-path rewrite
        m_ouo = re.search(r'ouo\.(?:io|press)/([A-Za-z0-9_-]+)', clean)
        if m_ouo and not clean.startswith("https://ouo.io/xreallcygo"):
            slug = m_ouo.group(1)
            if slug not in ("go", "fbc"):
                return f"https://ouo.io/xreallcygo/{slug}"

        return None

    def query_adshrink_api(self, adshnk_url: str) -> str | None:
        """
        Executes the internal AdShrink prototype init API handshake to retrieve destination.
        """
        try:
            api_url = "https://www.shrink-service.it/v3/api/prototype/init"
            normalized_uri = adshnk_url.replace("ashnk.com", "adshnk.com")
            payload = urllib.parse.urlencode({
                "req": "init",
                "uri": normalized_uri,
                "cookie_bypass_v1": "false",
            }).encode("utf-8")
            req = urllib.request.Request(
                api_url,
                data=payload,
                headers={
                    "User-Agent": self.user_agent,
                    "Origin": "https://adshnk.com",
                    "Referer": normalized_uri,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
            )
            with route_http.urlopen(req, timeout=self.request_timeout) as r:
                data = json.loads(r.read().decode("utf-8", errors="ignore"))

            if isinstance(data, dict):
                entry = data.get("0") or (data[0] if isinstance(data, list) and data else {})
                if isinstance(entry, dict):
                    dest = entry.get("destination")
                    if dest and _safe_url(dest, adshnk_url):
                        return html.unescape(dest).strip()
        except Exception as exc:
            logger.debug("AdShrink API query failed: %s", exc)

        return None

    def parse_rekonise_campaign(self, html_text: str, page_url: str) -> str | None:
        """
        Extracts unlocked target URL from Rekonise campaign page state.
        (Analogous to FastForward rekonise.js)
        """
        m = re.search(r'target_url\s*:\s*["\'](https?://[^"\']+)["\']', html_text)
        if m and _safe_url(m.group(1), page_url):
            return m.group(1).strip()

        m_json = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html_text, re.DOTALL)
        if m_json:
            try:
                state = json.loads(m_json.group(1))
                dest = state.get("campaign", {}).get("target_url")
                if dest and _safe_url(dest, page_url):
                    return dest.strip()
            except Exception:
                pass

        return None
