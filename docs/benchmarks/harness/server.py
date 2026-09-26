"""Range-capable deterministic HTTP/1.1 test server with network shaping.

Routes
  GET|HEAD /f/<size>/<seed>/<name>             deterministic file (see payload.py)
  GET      /hls/<count>/<segsize>/<seed>/index.m3u8   VOD media playlist
  GET|HEAD /hls/<count>/<segsize>/<seed>/seg<k>.ts   segment k (seed*1000003+k)
  GET      /static/<path>                      file from --static-dir (full body only)
  GET      /__stats                            JSON counters + server clock
  GET      /__reset                            zero the counters

Run standalone (e.g. on a second machine for LAN tests):
  python -m harness.server --port 8765 --conn-rate 2MiB --max-conns 4
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import payload
from .netprofile import NetProfile, Pacer, TokenBucket

_LAST_MODIFIED = formatdate(1_700_000_000, usegmt=True)


def hls_segment_seed(seed: int, index: int) -> int:
    return seed * 1_000_003 + index


class Stats:
    FIELDS = ("requests", "head_requests", "get_requests", "range_requests", "rejected_overlimit",
              "aborted", "dropped_by_profile", "keepalive_closed", "active", "peak_active", "bytes_sent")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            for name in self.FIELDS:
                setattr(self, name, 0)
            self.first_payload_at = None
            self.last_payload_at = None
            self.user_agents: dict[str, int] = {}

    def add(self, name: str, amount: int = 1) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + amount)

    def snapshot(self) -> dict:
        with self.lock:
            data = {name: getattr(self, name) for name in self.FIELDS}
            data.update(first_payload_at=self.first_payload_at, last_payload_at=self.last_payload_at,
                        user_agents=dict(self.user_agents), now=time.time())
            return data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "BenchServer"

    def log_message(self, *_args) -> None:  # keep the console quiet during runs
        return

    # ---- routing -------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head=True)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(head=False)

    def _dispatch(self, head: bool) -> None:
        stats = self.server.stats
        path = unquote(urlsplit(self.path).path)
        if path == "/__stats":
            return self._json(stats.snapshot() | {"profile": self.server.profile.to_dict()})
        if path == "/__reset":
            stats.reset()
            return self._json({"reset": True, "now": time.time()})
        stats.add("requests")
        stats.add("head_requests" if head else "get_requests")
        agent = self.headers.get("User-Agent", "")[:120]
        with stats.lock:
            stats.user_agents[agent] = stats.user_agents.get(agent, 0) + 1
        self.server.profile.delay()
        parts = [part for part in path.split("/") if part]
        try:
            if len(parts) == 4 and parts[0] == "f":
                return self._file(payload.parse_size(parts[1]), int(parts[2]), parts[3], head)
            if len(parts) == 5 and parts[0] == "hls":
                return self._hls(int(parts[1]), payload.parse_size(parts[2]), int(parts[3]), parts[4], head)
            if parts and parts[0] == "static" and self.server.static_dir:
                return self._static("/".join(parts[1:]), head)
        except ValueError:
            return self._empty(400)
        return self._empty(404)

    # ---- responses -----------------------------------------------------
    def _json(self, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status: int, extra: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _hls(self, count: int, segsize: int, seed: int, leaf: str, head: bool) -> None:
        if leaf == "index.m3u8":
            lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4",
                     "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
            for index in range(count):
                lines += ["#EXTINF:4.000,", f"seg{index}.ts"]
            body = ("\n".join(lines + ["#EXT-X-ENDLIST"]) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return
        if leaf.startswith("seg") and leaf.endswith(".ts"):
            index = int(leaf[3:-3])
            if 0 <= index < count:
                return self._file(segsize, hls_segment_seed(seed, index), leaf, head, "video/mp2t")
        self._empty(404)

    def _static(self, relative: str, head: bool) -> None:
        root = Path(self.server.static_dir).resolve()
        target = (root / relative).resolve()
        if root not in target.parents or not target.is_file():
            return self._empty(404)
        data = target.read_bytes()
        kind = "application/vnd.apple.mpegurl" if target.suffix == ".m3u8" else "application/octet-stream"
        streaming = not head and bool(data)
        if streaming and not self._acquire():
            self.server.stats.add("rejected_overlimit")
            return self._empty(self.server.profile.overlimit_status, {"Retry-After": "1"})
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if streaming:
            self._stream([memoryview(data)], len(data))

    def _file(self, size: int, seed: int, name: str, head: bool, kind: str = "application/octet-stream") -> None:
        profile, stats = self.server.profile, self.server.stats
        etag = f'"{size:x}-{seed:x}"'
        start, end, status = 0, size - 1, 200
        header = self.headers.get("Range", "")
        if_range = self.headers.get("If-Range")
        honour = profile.ranges and header.startswith("bytes=") and "," not in header
        if honour and if_range and if_range != etag and if_range != _LAST_MODIFIED:
            honour = False  # validator changed: RFC 9110 says send the full body
        if honour:
            first, _, last = header[6:].strip().partition("-")
            try:
                if first == "":  # suffix range: last N bytes
                    start, end = max(0, size - int(last)), size - 1
                else:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
            except ValueError:
                return self._empty(416, {"Content-Range": f"bytes */{size}"})
            if start >= size or end < start:
                return self._empty(416, {"Content-Range": f"bytes */{size}"})
            status = 206
        length = end - start + 1 if size else 0
        if not head and length and not self._acquire():
            stats.add("rejected_overlimit")
            return self._empty(profile.overlimit_status, {"Retry-After": "1"})
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", _LAST_MODIFIED)
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        if profile.ranges:
            self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            stats.add("range_requests")
        self.end_headers()
        if head or length == 0:
            return
        self._stream(payload.read_range(size, seed, start, end), length)

    def _acquire(self) -> bool:
        """Reserve a payload-connection slot atomically (enforces max_conns)."""
        stats, limit = self.server.stats, self.server.profile.max_conns
        with stats.lock:
            if limit and stats.active >= limit:
                return False
            stats.active += 1
            stats.peak_active = max(stats.peak_active, stats.active)
            return True

    def _stream(self, pieces, length: int) -> None:
        """Send payload pieces under the profile; caller already holds a slot."""
        profile, stats = self.server.profile, self.server.stats
        pacer = Pacer(profile.conn_rate)
        chunk = pacer.chunk_size()
        sent = 0
        try:
            for piece in pieces:
                for offset in range(0, len(piece), chunk):
                    part = piece[offset:offset + chunk]
                    if profile.drop_after_bytes and sent + len(part) > profile.drop_after_bytes:
                        stats.add("dropped_by_profile")
                        self.close_connection = True
                        return
                    pacer.before_send(len(part))
                    self.server.bucket.consume(len(part))
                    self.wfile.write(part)
                    sent += len(part)
                    now = time.time()
                    with stats.lock:
                        stats.bytes_sent += len(part)
                        stats.first_payload_at = stats.first_payload_at or now
                        stats.last_payload_at = now
        except (ConnectionError, OSError):
            stats.add("aborted")
            self.close_connection = True
        finally:
            with stats.lock:
                stats.active -= 1


class BenchServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256

    def __init__(self, host: str, port: int, profile: NetProfile, static_dir: str | None = None) -> None:
        super().__init__((host, port), Handler)
        self.profile = profile
        self.bucket = TokenBucket(profile.total_rate)
        self.stats = Stats()
        self.static_dir = static_dir
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"

    def handle_error(self, request, client_address) -> None:
        # A client closing an idle keep-alive connection is normal; count it
        # instead of printing a traceback. Anything else is still reported.
        if isinstance(sys.exc_info()[1], ConnectionError):
            self.stats.add("keepalive_closed")
            return
        super().handle_error(request, client_address)

    def start_background(self) -> "BenchServer":
        self.thread = threading.Thread(target=self.serve_forever, daemon=True, name="bench-server")
        self.thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to serve a LAN (trusted networks only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--conn-rate", default="0", help="per-connection cap, e.g. 2MiB (per second)")
    parser.add_argument("--total-rate", default="0", help="aggregate cap, e.g. 100MiB (per second)")
    parser.add_argument("--latency-ms", type=float, default=0.0)
    parser.add_argument("--jitter-ms", type=float, default=0.0)
    parser.add_argument("--max-conns", type=int, default=0)
    parser.add_argument("--overlimit-status", type=int, default=503)
    parser.add_argument("--no-ranges", action="store_true")
    parser.add_argument("--drop-after", default="0", help="drop each response after N bytes")
    parser.add_argument("--static-dir", default=None)
    args = parser.parse_args(argv)
    profile = NetProfile("cli", payload.parse_size(args.conn_rate), payload.parse_size(args.total_rate),
                         args.latency_ms, args.jitter_ms, args.max_conns, args.overlimit_status,
                         not args.no_ranges, payload.parse_size(args.drop_after))
    server = BenchServer(args.host, args.port, profile, args.static_dir)
    print(f"serving {server.base_url}  profile={json.dumps(profile.to_dict())}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
