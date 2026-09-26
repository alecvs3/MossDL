from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.custom_downloader import CustomAsyncBackend
from engine.limits import ResourceManager, SchedulerPolicy
from engine.models import ResolvedItem
from engine.rust_backend import RustTransferBackend


FIXTURE = bytes((index * 31) % 251 for index in range(8 * 1024 * 1024))
REQUESTS: dict[str, int] = {}
REQUEST_LOCK = threading.Lock()


class BenchmarkHandler(BaseHTTPRequestHandler):
    def _mode(self) -> str:
        return parse_qs(urlsplit(self.path).query).get("mode", ["clean"])[0]

    def _count(self) -> int:
        with REQUEST_LOCK:
            backend = parse_qs(urlsplit(self.path).query).get("backend", ["unknown"])[0]
            key = f"{self._mode()}:{backend}:{self.command}"
            REQUESTS[key] = REQUESTS.get(key, 0) + 1
            return REQUESTS[key]

    def do_HEAD(self):  # noqa: N802
        self._count()
        mode = self._mode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(FIXTURE)))
        if mode != "no-range":
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        count = self._count()
        mode = self._mode()
        if mode == "retry" and count == 1:
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        if mode == "no-range":
            start, end, status = 0, len(FIXTURE) - 1, 200
        else:
            value = self.headers.get("Range", "")
            start, end, status = 0, len(FIXTURE) - 1, 200
            if value.startswith("bytes="):
                begin, _, finish = value[6:].partition("-")
                start = int(begin)
                end = int(finish) if finish else end
                status = 206
        payload = FIXTURE[start : end + 1]
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Type", "application/octet-stream")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(FIXTURE)}")
        self.end_headers()
        if mode == "slow":
            time.sleep(0.02)
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


def run_case(name: str, url: str, root: Path, backend_name: str) -> dict:
    item = ResolvedItem("benchmark", url, f"{name}-{backend_name}.bin", size=len(FIXTURE), direct_url=url)
    destination = root / item.display_name
    root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    error = None
    if backend_name == "custom":
        backend = CustomAsyncBackend(ResourceManager(SchedulerPolicy(min_segment_size=1024 * 1024, max_segments_per_file=4)))
        try:
            asyncio.run(backend.download(item, root))
        except Exception as exc:  # benchmark records failures rather than hiding them
            error = str(exc)
    else:
        backend = RustTransferBackend(min_segment_size=1024 * 1024)
        if not backend.available():
            error = "Rust transfer-core unavailable"
        else:
            try:
                backend.download(item, root)
            except Exception as exc:
                error = str(exc)
    elapsed = time.perf_counter() - started
    result = {
        "case": name,
        "backend": backend_name,
        "seconds": round(elapsed, 4),
        "bytes": destination.stat().st_size if destination.exists() else 0,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest() if destination.exists() else None,
        "correct": destination.exists() and destination.read_bytes() == FIXTURE,
        "error": error,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare the Rust and Python transports on deterministic fixtures")
    parser.add_argument("--output", type=Path, default=Path(".test-artifacts") / "benchmark.json")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), BenchmarkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cases = {
            "clean-range": "clean",
            "no-range": "no-range",
            "slow-range": "slow",
            "retry": "retry",
        }
        results = []
        root = Path(".test-artifacts") / f"benchmark-run-{time.time_ns()}"
        root.mkdir(parents=True, exist_ok=True)
        for name, mode in cases.items():
            for backend_name in ("custom", "rust"):
                url = f"http://127.0.0.1:{server.server_port}/fixture?mode={mode}&backend={backend_name}"
                results.append(run_case(name, url, root / f"{name}-{backend_name}", backend_name))
        report = {
            "fixture_bytes": len(FIXTURE),
            "fixture_sha256": hashlib.sha256(FIXTURE).hexdigest(),
            "requests": REQUESTS,
            "results": results,
        }
        report["summary"] = {}
        for backend_name in ("custom", "rust"):
            backend_results = [result for result in results if result["backend"] == backend_name]
            completed = [result for result in backend_results if result["correct"]]
            report["summary"][backend_name] = {
                "cases": len(backend_results),
                "correct": len(completed),
                "mean_seconds": round(sum(result["seconds"] for result in completed) / len(completed), 4) if completed else None,
            }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        checked_backends = {"custom", "rust"}
        return 0 if all(result["correct"] for result in results if result["backend"] in checked_backends) else 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
