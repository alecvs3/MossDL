"""Network-condition profile for the test server, plus pacing primitives.

What this simulates honestly:
  * per-connection rate caps (the way many file hosts cap each TCP stream),
  * an aggregate cap shared by all connections (a host's total bandwidth),
  * per-request delay before the response (request/think latency),
  * a concurrent-connection limit answered with 503/429 (host connection caps),
  * connections dropped after N bytes (flaky hosts; exercises resume logic),
  * servers that ignore Range.

What it does NOT simulate: packet-level RTT, loss, or TCP window dynamics.
Per-request delay is not the same as a 150 ms path RTT because slow start
and the bandwidth-delay product are unaffected. For true RTT/loss use a
packet-level shaper in front of the server (toxiproxy latency toxic, clumsy,
or a Linux ``tc netem`` box) and record which one was used.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import asdict, dataclass


@dataclass
class NetProfile:
    name: str = "unthrottled"
    conn_rate: int = 0            # bytes/s per connection, 0 = unlimited
    total_rate: int = 0           # bytes/s across all connections, 0 = unlimited
    latency_ms: float = 0.0       # delay before each response's status line
    jitter_ms: float = 0.0        # uniform +/- jitter on latency_ms
    max_conns: int = 0            # concurrent payload connections, 0 = unlimited
    overlimit_status: int = 503   # status returned when max_conns is exceeded
    ranges: bool = True           # False: ignore Range, never advertise it
    drop_after_bytes: int = 0     # close each response after N payload bytes, 0 = never

    def to_dict(self) -> dict:
        return asdict(self)

    def delay(self) -> None:
        if self.latency_ms <= 0:
            return
        jitter = random.uniform(-self.jitter_ms, self.jitter_ms) if self.jitter_ms else 0.0
        time.sleep(max(0.0, self.latency_ms + jitter) / 1000.0)


class TokenBucket:
    """Blocking token bucket shared by all connections (aggregate cap)."""

    def __init__(self, rate: int, burst: int | None = None) -> None:
        self.rate = int(rate)
        self.capacity = float(burst or max(64 * 1024, self.rate // 10))
        self.tokens = self.capacity
        self.stamp = time.perf_counter()
        self.lock = threading.Lock()

    def consume(self, amount: int) -> None:
        if self.rate <= 0:
            return
        remaining = float(amount)
        while remaining > 0:
            with self.lock:
                now = time.perf_counter()
                self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
                self.stamp = now
                take = min(remaining, self.tokens)
                self.tokens -= take
                remaining -= take
                wait = remaining / self.rate if remaining > 0 else 0.0
            if wait > 0:
                time.sleep(min(wait, 0.05))


class Pacer:
    """Deadline pacing for one connection: never ahead of ``rate`` bytes/s."""

    def __init__(self, rate: int) -> None:
        self.rate = int(rate)
        self.start = time.perf_counter()
        self.sent = 0

    def before_send(self, amount: int) -> None:
        if self.rate <= 0:
            return
        due = self.start + (self.sent + amount) / self.rate
        wait = due - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        self.sent += amount

    def chunk_size(self, default: int = 256 * 1024) -> int:
        # Small chunks at low rates keep pacing smooth (~20 writes/s minimum).
        if self.rate <= 0:
            return default
        return max(4096, min(default, self.rate // 20))
