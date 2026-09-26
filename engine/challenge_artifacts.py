"""What a solved challenge leaves behind, who owns it, and where it may go.

A solve produces several different things that used to travel together as one
flat dict: a Cloudflare clearance cookie that is valid for a host from one
network identity, a single-use response token, form cookies tied to one
file's pending download, a direct URL, a user agent. The multipart failure
came from treating them as interchangeable. Each now has a kind with an owner,
a replay scope, a lifetime and a serialization rule, and only the reusable
clearance may ever cross from one task to another, and then only between
requests that share its identity (host, route, user agent, TLS profile).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

REUSABLE_CLEARANCE = "reusable_clearance"
SINGLE_USE_TOKEN = "single_use_token"
BROWSER_IDENTITY = "browser_identity"
FORM_SESSION = "form_session"
DIRECT_URL = "direct_url"
CONTINUATION = "continuation"
ANSWER_TEXT = "answer_text"


@dataclass(frozen=True)
class ArtifactPolicy:
    kind: str
    owner: str               # "identity" (host + route + UA + TLS) or "task" (one member only)
    crosses_tasks: bool      # may another task use it
    durable: bool            # may it be written to disk
    public: bool             # may it appear in UI/telemetry unredacted
    lifetime_seconds: float


POLICIES: dict[str, ArtifactPolicy] = {
    REUSABLE_CLEARANCE: ArtifactPolicy(REUSABLE_CLEARANCE, "identity", True, False, False, 2 * 3600),
    SINGLE_USE_TOKEN: ArtifactPolicy(SINGLE_USE_TOKEN, "task", False, False, False, 120),
    BROWSER_IDENTITY: ArtifactPolicy(BROWSER_IDENTITY, "identity", True, False, True, 2 * 3600),
    FORM_SESSION: ArtifactPolicy(FORM_SESSION, "task", False, False, False, 600),
    DIRECT_URL: ArtifactPolicy(DIRECT_URL, "task", False, False, False, 600),
    CONTINUATION: ArtifactPolicy(CONTINUATION, "task", False, False, False, 600),
    ANSWER_TEXT: ArtifactPolicy(ANSWER_TEXT, "task", False, False, False, 120),
}

# The only cookies that are clearance rather than per-file session state.
# Positive allowlist: anything not named here stays with the task that earned it.
CLEARANCE_COOKIES = frozenset({"cf_clearance", "__cf_bm", "_cfuvid", "__cflb", "__ddg1_", "__ddg2_", "__ddgid_", "__ddgmark_"})
_TOKEN_KEYS = frozenset({"token", "g-recaptcha-response", "h-captcha-response", "cf-turnstile-response", "turnstile_token", "response"})
_URL_KEYS = frozenset({"direct_url", "direct", "link", "download_url", "url"})
_CONTINUATION_KEYS = frozenset({"post_body", "form", "form_data", "continuation", "response_html", "file_code", "rand"})


def split_solution(solution: dict[str, Any] | None) -> dict[str, Any]:
    """A solver result sorted into artifact kinds."""
    parts: dict[str, Any] = {}
    if not isinstance(solution, dict):
        return parts
    cookies = dict(solution.get("cookies") or {})
    if solution.get("cf_clearance"):
        cookies.setdefault("cf_clearance", solution["cf_clearance"])
    clearance = {k: v for k, v in cookies.items() if k in CLEARANCE_COOKIES and v}
    session = {k: v for k, v in cookies.items() if k not in CLEARANCE_COOKIES and v}
    if clearance:
        parts[REUSABLE_CLEARANCE] = clearance
    if session:
        parts[FORM_SESSION] = session
    if solution.get("user_agent"):
        parts[BROWSER_IDENTITY] = str(solution["user_agent"])
    for key, value in solution.items():
        if not value:
            continue
        lowered = key.lower()
        if lowered in _TOKEN_KEYS:
            parts[SINGLE_USE_TOKEN] = value
        elif lowered in _URL_KEYS:
            parts[DIRECT_URL] = value
        elif lowered in _CONTINUATION_KEYS:
            parts.setdefault(CONTINUATION, {})[key] = value
        elif lowered == "text":
            parts[ANSWER_TEXT] = value
    return parts


def shareable_session(solution: dict[str, Any] | None) -> dict[str, Any]:
    """What of a solve may be offered to other tasks: clearance cookies and the
    user agent they were earned with. Tokens, URLs and form state never."""
    parts = split_solution(solution)
    clearance = parts.get(REUSABLE_CLEARANCE) or {}
    return {
        "cf_clearance": clearance.get("cf_clearance"),
        "cookies": clearance or None,
        "user_agent": parts.get(BROWSER_IDENTITY),
    }


def identity(host: str, *, route: str | None, user_agent: str | None, impersonate: str | None) -> str:
    """A stable fingerprint of the network identity clearance is bound to."""
    raw = "|".join([(host or "").lower().lstrip("."), route or "direct", user_agent or "", impersonate or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def reuse_decision(stored: dict[str, str | None], requested: dict[str, str | None]) -> tuple[bool, str]:
    """May clearance earned under `stored` identity be sent under `requested`?

    Positive: every recorded component must match. An unknown user agent on the
    request is allowed (the stored one is then used); anything else is not.
    """
    if (stored.get("route") or "direct") != (requested.get("route") or "direct"):
        return False, "route_mismatch"
    if (stored.get("impersonate") or "") != (requested.get("impersonate") or ""):
        return False, "tls_profile_mismatch"
    if requested.get("user_agent") and stored.get("user_agent") and stored["user_agent"] != requested["user_agent"]:
        return False, "user_agent_mismatch"
    return True, "identity_match"


def redacted_summary(solution: dict[str, Any] | None) -> dict[str, Any]:
    """Safe to log: which kinds of artifact a solve produced, never their values."""
    parts = split_solution(solution)
    return {kind: (len(value) if isinstance(value, (str, dict, list)) else True) for kind, value in parts.items()}
