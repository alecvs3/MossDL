from __future__ import annotations

import json
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable
from urllib.parse import urlsplit
from uuid import uuid4


PROTOCOL_VERSION = "browser-capture/1"
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_BATCH_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 256
MAX_REQUEST_ID_BYTES = 128
CAPABILITIES = ("candidate-batches", "review", "direct-import", "replay", "session-sync", "challenge-return")


class ProtocolError(ValueError):
    """A browser/native-host envelope failed the versioned contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def read_exact(stream: BinaryIO, n: int) -> bytes:
    chunks = []
    total = 0
    while total < n:
        chunk = stream.read(n - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def read_message(stream: BinaryIO, max_bytes: int = MAX_FRAME_BYTES) -> dict[str, Any] | None:
    header = read_exact(stream, 4)
    if not header:
        return None
    if len(header) != 4:
        raise ValueError("truncated native-messaging frame")
    length = struct.unpack("<I", header)[0]
    if length > max_bytes:
        raise ProtocolError("frame_too_large", "browser message exceeds the frame limit")
    payload = read_exact(stream, length)
    if len(payload) != length:
        raise ValueError("truncated native-messaging payload")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ProtocolError("invalid_json", "browser message must be an object")
    return value


def write_message(stream: BinaryIO, value: dict[str, Any]) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large", "browser response exceeds the frame limit")
    stream.write(struct.pack("<I", len(payload)) + payload)
    stream.flush()


def _bounded_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("invalid_envelope", f"{field} is required")
    if len(value.encode("utf-8")) > MAX_REQUEST_ID_BYTES:
        raise ProtocolError("invalid_envelope", f"{field} is too long")
    return value.strip()


def validate_envelope(value: dict[str, Any], *, state: str = "awaiting_handshake") -> dict[str, Any]:
    """Validate and return a bounded native-messaging protocol envelope."""
    if not isinstance(value, dict):
        raise ProtocolError("invalid_envelope", "browser envelope must be an object")
    if value.get("version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_version", "unsupported browser capture protocol version")
    message_type = value.get("type")
    if message_type not in {"hello", "candidate_batch", "status", "events_since", "acknowledge", "session_sync", "challenge_return"}:
        raise ProtocolError("unsupported_message", "unsupported browser capture message type")
    request_id = _bounded_text(value.get("request_id"), "request_id")
    if message_type == "hello":
        if state != "awaiting_handshake":
            raise ProtocolError("invalid_state", "handshake is only valid before connection readiness")
        capabilities = value.get("capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
            raise ProtocolError("invalid_envelope", "capabilities must be a list of strings")
        origin = value.get("origin") or {}
        if not isinstance(origin, dict):
            raise ProtocolError("invalid_envelope", "origin must be an object")
        extension_origin = _bounded_text(origin.get("extension_origin"), "origin.extension_origin")
        return {**value, "request_id": request_id, "origin": {
            "extension_origin": extension_origin,
            "page_origin": str(origin.get("page_origin") or "")[:512],
        }, "capabilities": sorted({str(item) for item in capabilities})}
    if state != "ready":
        raise ProtocolError("handshake_required", "browser capture handshake is required")
    if message_type == "session_sync":
        session = value.get("session") or {}
        if not isinstance(session, dict):
            raise ProtocolError("invalid_envelope", "session must be an object")
        domain = _bounded_text(session.get("domain"), "session.domain")
        cookies = session.get("cookies")
        if not isinstance(cookies, list):
            raise ProtocolError("invalid_envelope", "cookies must be a list")
        origin = value.get("origin") or {}
        return {**value, "request_id": request_id,
                "origin": {"extension_origin": str(origin.get("extension_origin") or "")[:512],
                           "page_origin": str(origin.get("page_origin") or "")[:512]},
                "session": {"domain": domain, "page_url": str(session.get("page_url") or "")[:2048],
                            "cookies": cookies[:500], "user_agent": str(session.get("user_agent") or "")[:512]}}
    if message_type == "challenge_return":
        # Checked field by field by the engine's handoff broker (ticket, origin,
        # profile, generation, size, cookie scope); here only its shape.
        handoff = value.get("handoff")
        if not isinstance(handoff, dict):
            raise ProtocolError("invalid_envelope", "handoff must be an object")
        return {**value, "request_id": request_id, "handoff": handoff}
    if message_type == "status":
        params = value.get("params") or {}
        if not isinstance(params, dict):
            raise ProtocolError("invalid_envelope", "params must be an object")
        return {**value, "request_id": request_id, "params": params}
    if message_type == "events_since":
        params = value.get("params") or {}
        if not isinstance(params, dict):
            raise ProtocolError("invalid_envelope", "params must be an object")
        return {**value, "request_id": request_id, "params": params}
    if message_type == "acknowledge":
        params = value.get("params") or {}
        if not isinstance(params, dict):
            raise ProtocolError("invalid_envelope", "params must be an object")
        return {**value, "request_id": request_id, "params": params}
    batch_id = _bounded_text(value.get("batch_id"), "batch_id")
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ProtocolError("invalid_batch", "candidate batch must contain candidates")
    if len(candidates) > MAX_CANDIDATES:
        raise ProtocolError("batch_too_large", "candidate batch exceeds the candidate limit")
    if len(json.dumps(value, separators=(",", ":")).encode("utf-8")) > MAX_BATCH_BYTES:
        raise ProtocolError("batch_too_large", "candidate batch exceeds the payload limit")
    if any(not isinstance(item, dict) for item in candidates):
        raise ProtocolError("invalid_batch", "candidate entries must be objects")
    origin = value.get("origin") or {}
    if not isinstance(origin, dict):
        raise ProtocolError("invalid_envelope", "origin must be an object")
    return {**value, "request_id": request_id, "batch_id": batch_id,
            "origin": {"extension_origin": str(origin.get("extension_origin") or "")[:512],
                        "page_origin": str(origin.get("page_origin") or "")[:512]},
            "candidates": candidates}


def protocol_error(exc: Exception, request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a stable, secret-free error envelope."""
    code = getattr(exc, "code", "invalid_request")
    message = getattr(exc, "message", str(exc) if str(exc) else "browser capture request rejected")
    return {"version": PROTOCOL_VERSION, "type": "error", "request_id": request.get("request_id") if request else None,
            "batch_id": request.get("batch_id") if request else None,
            "error": {"code": code, "message": str(message)[:256]}}


@dataclass
class NativeProtocolSession:
    """Connection state machine for one native-messaging stream."""

    dispatch: Callable[[str, dict[str, Any]], Any]
    connection_id: str = field(default_factory=lambda: str(uuid4()))
    state: str = "awaiting_handshake"
    acknowledged: set[tuple[str, str]] = field(default_factory=set)
    acknowledgement_results: dict[tuple[str, str], Any] = field(default_factory=dict)
    session_ref: str | None = None

    def _ensure_session(self, ref: str | None) -> None:
        if not ref:
            return
        broker = getattr(getattr(self.dispatch, "__self__", None), "session_broker", None)
        if broker is not None and broker.status(str(ref)) is None:
            broker.register(str(ref), "browser")

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        envelope = validate_envelope(message, state=self.state)
        if envelope["type"] == "hello":
            self.state = "ready"
            self.session_ref = envelope.get("session_ref")
            self._ensure_session(self.session_ref)
            return {"version": PROTOCOL_VERSION, "type": "hello_ack", "request_id": envelope["request_id"],
                    "connection_id": self.connection_id, "state": self.state,
                    "capabilities": list(CAPABILITIES)}
        if envelope["type"] == "status":
            result = self.dispatch("api_info", envelope.get("params", {}))
            return {"version": PROTOCOL_VERSION, "type": "status_ack", "request_id": envelope["request_id"],
                    "connection_id": self.connection_id, "result": result}
        if envelope["type"] == "events_since":
            result = self.dispatch("list_events", envelope.get("params", {}))
            return {"version": PROTOCOL_VERSION, "type": "events_since_ack", "request_id": envelope["request_id"],
                    "connection_id": self.connection_id, "result": result}
        if envelope["type"] == "acknowledge":
            identity = ("ack", envelope["request_id"])
            if identity in self.acknowledged:
                return {"version": PROTOCOL_VERSION, "type": "acknowledge_ack", "request_id": envelope["request_id"],
                        "connection_id": self.connection_id, "status": "replayed",
                        "result": self.acknowledgement_results.get(identity)}
            result = self.dispatch("ack_event", envelope.get("params", {}))
            self.acknowledged.add(identity)
            self.acknowledgement_results[identity] = result
            return {"version": PROTOCOL_VERSION, "type": "acknowledge_ack", "request_id": envelope["request_id"],
                    "connection_id": self.connection_id, "status": "accepted", "result": result}
        if envelope["type"] == "challenge_return":
            result = self.dispatch("captcha_handoff_return", {"handoff": envelope["handoff"]})
            return {"version": PROTOCOL_VERSION, "type": "challenge_return_ack", "request_id": envelope["request_id"],
                    "connection_id": self.connection_id, "result": result}
        if envelope["type"] == "session_sync":
            session_data = envelope.get("session", {})
            domain = str(session_data.get("domain", "")).lower().strip()
            cookies = session_data.get("cookies", [])
            ref = f"browser-session:{domain}"
            broker = getattr(getattr(self.dispatch, "__self__", None), "session_broker", None)
            if broker is not None:
                broker.register(ref, domain, account_ref=f"browser:{domain}")
            service = getattr(self.dispatch, "__self__", None)
            cookie_dict: dict[str, str] = {}
            if service is not None:
                cookie_dict = {
                    str(c.get("name")): str(c.get("value"))
                    for c in cookies
                    if isinstance(c, dict) and c.get("name") and c.get("value")
                }
                if hasattr(service, "set_host_session"):
                    # Through the allowlist: only clearance cookies are kept, bound to
                    # the browser's own (direct) network identity.
                    service.set_host_session(domain, {
                        "cookies": cookie_dict,
                        "cf_clearance": cookie_dict.get("cf_clearance"),
                        "user_agent": session_data.get("user_agent") or None,
                        "route_profile_id": "direct",
                    }, ttl=86400.0)
                events = getattr(service, "events", None)
                if events is not None:
                    events.emit("BrowserSessionSynced", None, {
                        "domain": domain,
                        "cookie_count": len(cookie_dict),
                        "reference": ref,
                    })
            return {
                "version": PROTOCOL_VERSION,
                "type": "session_sync_ack",
                "request_id": envelope["request_id"],
                "connection_id": self.connection_id,
                "status": "accepted",
                "result": {"domain": domain, "cookie_count": len(cookie_dict), "reference": ref},
            }
        identity = (envelope["batch_id"], envelope["request_id"])
        if identity in self.acknowledged:
            return {"version": PROTOCOL_VERSION, "type": "ack", "request_id": envelope["request_id"],
                    "batch_id": envelope["batch_id"], "connection_id": self.connection_id,
                    "status": "replayed", "result": self.acknowledgement_results.get(identity)}
        batch_session = envelope.get("session_ref") or self.session_ref
        if batch_session:
            envelope["session_ref"] = batch_session
            self._ensure_session(batch_session)
        result = self.dispatch("browser_capture_batch", envelope)
        self.acknowledged.add(identity)
        self.acknowledgement_results[identity] = result
        return {"version": PROTOCOL_VERSION, "type": "ack", "request_id": envelope["request_id"],
                "batch_id": envelope["batch_id"], "connection_id": self.connection_id,
                "status": "accepted", "result": result}

    def error(self, exc: Exception, request: dict[str, Any] | None = None) -> dict[str, Any]:
        if isinstance(exc, ProtocolError) and exc.code == "handshake_required":
            self.state = "failed"
        return protocol_error(exc, request)

    def close(self) -> None:
        self.state = "closed"


def validate_capture(value: dict[str, Any]) -> dict[str, Any]:
    url = str(value.get("url", ""))
    if urlsplit(url).scheme not in {"http", "https", "ftp"}:
        raise ValueError("browser capture URL must use http, https, or ftp")
    headers = {str(k): str(v) for k, v in (value.get("headers") or {}).items()
               if str(k).lower() not in {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}}
    # Cookies remain an operation-scoped secret and are never persisted by this boundary.
    return {"url": url, "display_name": value.get("display_name"), "referrer": value.get("referrer"),
            "headers": headers, "credential_ref": value.get("credential_ref"),
            "page_context": value.get("page_context") or {}}


def serve_native(data_dir: str, *, input_stream: BinaryIO | None = None,
                 output_stream: BinaryIO | None = None, service: Any | None = None) -> None:
    if sys.platform == "win32":
        try:
            import msvcrt
            import os
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        except Exception:
            pass
    from .service import EngineService
    owned_service = service is None
    service = service or EngineService(data_dir)
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    protocol = NativeProtocolSession(service.dispatch)
    try:
        while True:
            message = read_message(input_stream)
            if message is None:
                break
            try:
                write_message(output_stream, protocol.handle(message))
            except Exception as exc:
                write_message(output_stream, protocol.error(exc, message))
    finally:
        protocol.close()
        if owned_service:
            service.close()


if __name__ == "__main__":
    serve_native(sys.argv[sys.argv.index("--data-dir") + 1] if "--data-dir" in sys.argv else ".transfer-manager")
