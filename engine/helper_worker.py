from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


MAX_LINE_BYTES = 16 * 1024
HEARTBEAT_SECONDS = 0.25


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def _reader(stream, output: queue.Queue[tuple[str, bytes | None]]) -> None:
    try:
        for line in iter(stream.readline, b""):
            output.put(("line", line))
    finally:
        output.put(("eof", None))


def run(request: dict[str, Any], executable: Path) -> int:
    operation = request.get("operation")
    params = request.get("params") or {}
    if not isinstance(operation, str) or not isinstance(params, dict):
        _emit({"type": "error", "status": "unsupported", "message": "malformed helper request"})
        return 2
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1"}
    credential = request.get("credential_value")
    if isinstance(credential, str) and credential:
        env["HELPER_CREDENTIAL"] = credential
    try:
        command = [str(executable), "--operation", operation]
        if executable.suffix.lower() == ".py":
            command = [sys.executable, str(executable), "--operation", operation]
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=str(executable.parent),
        )
        assert child.stdin and child.stdout and child.stderr
        child.stdin.write(json.dumps(params, separators=(",", ":")).encode() + b"\n")
        child.stdin.close()
    except OSError as exc:
        _emit({"type": "error", "status": "crashed", "message": f"helper launch failed: {type(exc).__name__}"})
        return 127

    events: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
    threading.Thread(target=_reader, args=(child.stdout, events), daemon=True).start()
    threading.Thread(target=_reader, args=(child.stderr, events), daemon=True).start()
    stdout_bytes = 0
    stderr_bytes = 0
    done = False
    eof_count = 0
    last_heartbeat = time.monotonic()
    while child.poll() is None or eof_count < 2:
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            _emit({"type": "heartbeat", "at": now})
            last_heartbeat = now
        try:
            kind, raw = events.get(timeout=0.05)
        except queue.Empty:
            continue
        if kind == "eof":
            eof_count += 1
            continue
        assert raw is not None
        if len(raw) > MAX_LINE_BYTES:
            _emit({"type": "error", "status": "output_limit", "message": "helper line exceeded limit"})
            child.kill()
            break
        stdout_bytes += len(raw)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _emit({"type": "error", "status": "malformed_progress", "message": "helper emitted non-json output"})
            continue
        if not isinstance(value, dict) or value.get("type") not in {"progress", "result", "heartbeat"}:
            _emit({"type": "error", "status": "malformed_progress", "message": "helper emitted an invalid message"})
            continue
        _emit(value)
        if value.get("type") == "result":
            done = True
    code = child.wait()
    if not done and code != 0:
        _emit({"type": "error", "status": "crashed", "message": f"helper exited with code {code}"})
    elif not done:
        _emit({"type": "error", "status": "crashed", "message": "helper exited without a result"})
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.readline(MAX_LINE_BYTES + 1))
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        return run(request, Path(args.executable).resolve())
    except Exception as exc:
        _emit({"type": "error", "status": "unsupported", "message": f"worker request failed: {type(exc).__name__}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
