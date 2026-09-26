"""Finishing a challenge in the user's own browser, safely.

When no automatic solver can (or did) answer, the user can open the real page
in their browser. Each opening gets a ticket: 256 random bits, single use,
expiring, bound to the task, the challenge generation, the page's origin and
the browser profile. The ticket travels in the URL fragment (never sent to the
site); the extension returns the widget's answer and the origin's clearance
cookies with it over native messaging. A return is accepted only if every
binding matches, and even then it only moves the challenge to `verifying`: the
resumed task and the site decide (challenge_verifier). Tickets are stored as
hashes and never logged.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from . import challenge_lifecycle as lifecycle
from .telemetry import telemetry_bus

HANDOFF_VERSION = "mossdl-handoff/1"
TICKET_TTL = 15 * 60
MAX_RETURN_BYTES = 64 * 1024
MAX_TOKEN_CHARS = 8192
MAX_COOKIES = 50
FRAGMENT_KEY = "mossdl-handoff"

# Every way a handoff can end, named for the UI and the tests.
ACCEPTED, UNAVAILABLE, DENIED, EXPIRED = "accepted", "unavailable", "denied", "expired"
REPLAYED, MISMATCHED, CANCELLED, INVALID, UNKNOWN = "replayed", "mismatched", "cancelled", "invalid", "unknown_ticket"


@dataclass
class _Handoff:
    challenge_id: str
    task_id: str | None
    generation: int
    origin: str
    profile: str
    expires_at: float
    state: str = "open"   # open | consumed | cancelled
    group_id: str = ""    # a multipart package: one foreground handoff at a time


def _digest(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def origin_of(url: str) -> str | None:
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname.lower()}{port}"


def _cookie_in_scope(cookie_domain: str, host: str) -> bool:
    domain = (cookie_domain or "").lower().lstrip(".")
    return bool(domain) and (host == domain or host.endswith("." + domain))


class HandoffBroker:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._lock = threading.Lock()
        self._handoffs: dict[str, _Handoff] = {}

    def _outcome(self, outcome: str, challenge_id: str | None, detail: str = "") -> dict[str, Any]:
        telemetry_bus.record(level="INFO" if outcome == ACCEPTED else "WARN", subsystem="engine:captcha",
                             message=f"[HANDOFF_{outcome.upper()}] {challenge_id or '?'}{': ' + detail if detail else ''}",
                             context={"challenge_id": challenge_id, "outcome": outcome, "detail": detail}, tier="engine")
        return {"outcome": outcome, "challenge_id": challenge_id, "detail": detail}

    def open(self, challenge_id: str, generation: int | None = None, profile: str = "default") -> dict[str, Any]:
        """A ticket and the page to open, or an explicit reason there is none."""
        challenge = self.manager.get_challenge(challenge_id)
        refused = lifecycle.command_outcome(challenge, generation)
        if refused:
            return self._outcome(UNAVAILABLE if refused == "unknown_challenge" else DENIED, challenge_id, refused)
        page = (challenge.params or {}).get("page_url") or (challenge.params or {}).get("url") or ""
        origin = origin_of(page)
        if origin is None:
            # Only the challenge's own http(s) page is ever opened.
            return self._outcome(UNAVAILABLE, challenge_id, "the challenge has no web page to open")
        group = str((challenge.params or {}).get("group_id") or "")
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            now = time.time()
            if group and any(h.group_id == group and h.challenge_id != challenge_id and h.state == "open"
                             and h.expires_at > now for h in self._handoffs.values()):
                # MULTI-05: one browser handoff per package; later parts stay visible.
                return self._outcome(DENIED, challenge_id, "another part of this package is open in the browser; finish it first")
            # One open handoff per challenge: a new one cancels the old ticket.
            for handoff in self._handoffs.values():
                if handoff.challenge_id == challenge_id and handoff.state == "open":
                    handoff.state = "cancelled"
            self._handoffs[_digest(ticket)] = _Handoff(challenge_id, challenge.task_id, lifecycle.generation_of(challenge),
                                                       origin, profile, time.time() + TICKET_TTL, group_id=group)
        if lifecycle.state_of(challenge) != lifecycle.MANUAL:
            lifecycle.advance(challenge, lifecycle.MANUAL, "opened in the browser")
        self._outcome("opened", challenge_id)
        url = page.split("#", 1)[0] + "#" + urllib.parse.urlencode(
            {FRAGMENT_KEY: ticket, "mossdl-c": challenge_id, "mossdl-g": lifecycle.generation_of(challenge)})
        return {"outcome": "opened", "challenge_id": challenge_id, "url": url, "expires_at": time.time() + TICKET_TTL}

    def cancel(self, challenge_id: str) -> None:
        with self._lock:
            for handoff in self._handoffs.values():
                if handoff.challenge_id == challenge_id and handoff.state == "open":
                    handoff.state = "cancelled"

    def _validate(self, message: Any) -> tuple[str | None, str]:
        if not isinstance(message, dict):
            return INVALID, "not an object"
        if len(json.dumps(message, separators=(",", ":")).encode("utf-8")) > MAX_RETURN_BYTES:
            return INVALID, "too large"
        if message.get("version") != HANDOFF_VERSION:
            return INVALID, "unsupported handoff version"
        for key in ("ticket", "challenge_id", "origin", "profile"):
            if not isinstance(message.get(key), str) or not message[key] or len(message[key]) > 512:
                return INVALID, f"{key} missing or malformed"
        if not isinstance(message.get("generation"), int):
            return INVALID, "generation missing"
        token = message.get("token")
        if token is not None and (not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS):
            return INVALID, "token malformed"
        cookies = message.get("cookies") or []
        if not isinstance(cookies, list) or len(cookies) > MAX_COOKIES or any(
                not isinstance(c, dict) or not isinstance(c.get("name"), str) or not isinstance(c.get("value"), str)
                or len(c["name"]) > 256 or len(c["value"]) > 4096 for c in cookies):
            return INVALID, "cookies malformed"
        if not token and not cookies:
            return INVALID, "nothing returned"
        return None, ""

    def redeem(self, message: Any) -> dict[str, Any]:
        """Check a browser return against its ticket; on success hand the answer to the task."""
        problem, detail = self._validate(message)
        challenge_id = message.get("challenge_id") if isinstance(message, dict) else None
        if problem:
            return self._outcome(problem, challenge_id, detail)
        with self._lock:
            handoff = self._handoffs.get(_digest(message["ticket"]))
            if handoff is None:
                return self._outcome(UNKNOWN, challenge_id, "no such ticket")
            if handoff.state == "consumed":
                return self._outcome(REPLAYED, challenge_id, "ticket already used")
            if handoff.state == "cancelled":
                return self._outcome(CANCELLED, challenge_id, "handoff was cancelled")
            if time.time() > handoff.expires_at:
                return self._outcome(EXPIRED, challenge_id, "ticket expired")
            challenge = self.manager.get_challenge(handoff.challenge_id)
            if challenge is None or challenge_id != handoff.challenge_id:
                return self._outcome(MISMATCHED, challenge_id, "different challenge")
            if message["generation"] != handoff.generation or lifecycle.generation_of(challenge) != handoff.generation:
                return self._outcome(MISMATCHED, challenge_id, "stale generation")
            if message["origin"] != handoff.origin:
                return self._outcome(MISMATCHED, challenge_id, "wrong origin")
            if message["profile"] != handoff.profile:
                return self._outcome(MISMATCHED, challenge_id, "wrong browser profile")
            host = urllib.parse.urlsplit(handoff.origin).hostname or ""
            cookies = message.get("cookies") or []
            if any(not _cookie_in_scope(str(c.get("domain") or host), host) for c in cookies):
                return self._outcome(MISMATCHED, challenge_id, "cookie outside the page's site")
            handoff.state = "consumed"
            if handoff.group_id:
                # The package's next part may now be opened.
                for other in self._handoffs.values():
                    if other.group_id == handoff.group_id and other.state == "open" and other is not handoff:
                        other.state = "cancelled"
        solution: dict[str, Any] = {"cookies": {c["name"]: c["value"] for c in cookies}}
        if message.get("token"):
            solution["token"] = message["token"]
        if message.get("user_agent"):
            solution["user_agent"] = str(message["user_agent"])[:512]
        if solution["cookies"].get("cf_clearance"):
            solution["cf_clearance"] = solution["cookies"]["cf_clearance"]
        if not self.manager.solve_challenge(handoff.challenge_id, solution, solver_id="browser_handoff"):
            return self._outcome(DENIED, challenge_id, "the challenge is no longer waiting for an answer")
        result = self._outcome(ACCEPTED, challenge_id)
        result["task_id"] = handoff.task_id
        return result
