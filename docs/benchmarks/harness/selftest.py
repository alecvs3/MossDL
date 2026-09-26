"""Harness self-test: server correctness and the server's own throughput ceiling.

    python -m harness.selftest                 # correctness + 1 GiB / 8-connection ceiling
    python -m harness.selftest --size 256MiB --connections 16

1. Correctness: fetch a file as 7 uneven ranges, reassemble, compare SHA-256
   with payload.sha256; check 416 on an out-of-range request and If-Range
   fallback to 200.
2. Ceiling: a separate *process* pulls the file over N ranged connections and
   discards the bytes. Any client result near this number is limited by the
   harness, not the client -- report it as "server-bound" (see METHODOLOGY.md).
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import multiprocessing
import threading
import time
from urllib.parse import urlsplit

from . import payload
from .netprofile import NetProfile
from .server import BenchServer


def _get(base: str, path: str, headers: dict | None = None) -> tuple[int, bytes]:
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, body


def correctness(base: str) -> list[str]:
    size, seed = 5 * payload.BLOCK + 12345, 99
    path = f"/f/{size}/{seed}/check.bin"
    cuts = [0, 1, 700_000, payload.BLOCK, 3 * payload.BLOCK + 17, 4 * payload.BLOCK, size - 3, size]
    data = b""
    for start, end in zip(cuts, cuts[1:]):
        status, body = _get(base, path, {"Range": f"bytes={start}-{end - 1}"})
        assert status == 206, f"expected 206, got {status}"
        data += body
    problems = []
    if hashlib.sha256(data).hexdigest() != payload.sha256(size, seed):
        problems.append("ranged reassembly digest mismatch")
    if _get(base, path, {"Range": f"bytes={size}-"})[0] != 416:
        problems.append("out-of-range request did not return 416")
    if _get(base, path, {"Range": "bytes=0-9", "If-Range": '"stale"'})[0] != 200:
        problems.append("If-Range with a stale validator did not fall back to 200")
    return problems


def _pull(base: str, path: str, start: int, end: int, counter) -> None:
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=60)
    connection.request("GET", path, headers={"Range": f"bytes={start}-{end}"})
    response = connection.getresponse()
    received = 0
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        received += len(chunk)
    with counter.get_lock():
        counter.value += received


def _client_process(base: str, size: int, connections: int, counter) -> None:
    path = f"/f/{size}/3/ceiling.bin"
    step = size // connections
    threads = [threading.Thread(target=_pull, args=(base, path, i * step,
                                                    size - 1 if i == connections - 1 else (i + 1) * step - 1, counter))
               for i in range(connections)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def ceiling(base: str, size: int, connections: int) -> float:
    counter = multiprocessing.Value("q", 0)
    started = time.perf_counter()
    worker = multiprocessing.Process(target=_client_process, args=(base, size, connections, counter))
    worker.start()
    worker.join()
    elapsed = time.perf_counter() - started
    if counter.value != size:
        raise RuntimeError(f"ceiling pull received {counter.value} of {size} bytes")
    return size / elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", default="1GiB")
    parser.add_argument("--connections", type=int, default=8)
    parser.add_argument("--server-url", default=None, help="test an external server instead of an in-process one")
    args = parser.parse_args(argv)
    server = None if args.server_url else BenchServer("127.0.0.1", 0, NetProfile()).start_background()
    base = args.server_url or server.base_url
    try:
        problems = correctness(base)
        print("correctness:", "OK" if not problems else "; ".join(problems))
        size = payload.parse_size(args.size)
        rate = ceiling(base, size, args.connections)
        print(f"server ceiling: {rate / 2**20:.1f} MiB/s ({rate * 8 / 1e9:.2f} Gbit/s) "
              f"with {args.connections} connections, {size / 2**20:.0f} MiB (loopback, in-process server)")
        return 0 if not problems else 1
    finally:
        if server:
            server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
