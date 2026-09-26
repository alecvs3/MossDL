from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .service import (EngineService, _redact_diagnostic, authenticate_headless_token,
                      headless_required_scope, pre_dispatch)


_CAPTURE_METHOD_SCOPES = {
    "browser_capture_batch": "read",
    "capture_batch": "read",
    "capture_candidates": "read",
    "browser_capture": "read",
    "capture_review_list": "read",
    "capture_inbox_list": "read",
    "capture_review_get": "read",
    "capture_batch_get": "read",
    "capture_import": "enqueue",
    "capture_review_import": "enqueue",
    "browser_import": "enqueue",
}

def required_scope(method: str) -> str | None:
    """Compatibility export for the shared headless method matrix."""
    scope = headless_required_scope(method)
    if scope is not None:
        return scope
    if method.startswith(("browser_", "capture_")):
        return None
    return None


def create_http_server(data_dir: str, host: str = "127.0.0.1", port: int = 8787,
                       token: str | None = None, service: EngineService | None = None) -> tuple[ThreadingHTTPServer, EngineService]:
    api_token = token or os.environ.get("TRANSFER_MANAGER_API_TOKEN")
    if not api_token:
        raise ValueError("TRANSFER_MANAGER_API_TOKEN or an explicit API token is required")
    if host not in {"127.0.0.1", "localhost", "::1"} and os.environ.get("TRANSFER_MANAGER_ALLOW_LAN") != "1":
        raise ValueError("LAN API binding requires TRANSFER_MANAGER_ALLOW_LAN=1")
    svc = service or EngineService(data_dir)

    class Handler(BaseHTTPRequestHandler):
        def _auth_context(self, request_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
            value = self.headers.get("Authorization", "")
            if value.startswith("Bearer "):
                token_str = value[7:].strip()
                auth = authenticate_headless_token(svc, token_str, api_token)
                if auth is not None:
                    return auth
            if isinstance(request_payload, dict):
                auth_val = request_payload.get("auth")
                tok = None
                if isinstance(auth_val, dict):
                    tok = auth_val.get("token") or auth_val.get("bearer")
                elif isinstance(auth_val, str):
                    tok = auth_val
                if not tok:
                    tok = request_payload.get("token")
                if tok:
                    return authenticate_headless_token(svc, str(tok), api_token)
            return None

        def _write(self, status: int, value: Any) -> None:
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path != "/health":
                self._write(404, {"error": "not found"})
                return
            self._write(200, svc.dispatch("health", {}))

        def do_POST(self):  # noqa: N802
            if self.path != "/rpc":
                self._write(404, {"error": "not found"})
                return
            length = min(int(self.headers.get("Content-Length", "0")), 4 * 1024 * 1024)
            raw = self.rfile.read(length).decode("utf-8")
            try:
                request = json.loads(raw) if raw else {}
            except Exception:
                self._write(400, {"jsonrpc": "2.0", "id": None, "error": {"type": "ValueError", "message": "invalid JSON"}})
                return

            auth_context = self._auth_context(request if isinstance(request, dict) else None)
            if auth_context is None:
                self._write(401, {"error": "unauthorized"})
                return
            try:
                result = pre_dispatch(svc, request, auth_context)
                self._write(200, {"jsonrpc": "2.0", "id": request.get("id"), "result": result})
            except Exception as exc:
                self._write(200, {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {
                    "type": type(exc).__name__, "message": _redact_diagnostic(str(exc))}})

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    return server, svc


def serve_http(data_dir: str, host: str = "127.0.0.1", port: int = 8787, token: str | None = None) -> None:
    server, service = create_http_server(data_dir, host=host, port=port, token=token)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()

