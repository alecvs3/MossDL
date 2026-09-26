"""Benchmark scenario catalogue. See METHODOLOGY.md for the rationale of each."""

from __future__ import annotations

from dataclasses import dataclass, field

from .netprofile import NetProfile
from .payload import parse_size

MiB = 1024 * 1024


@dataclass
class FileSpec:
    name: str
    size: int
    seed: int


@dataclass
class Scenario:
    name: str
    kind: str                          # http | hls | resume | public | idle
    description: str
    files: list[FileSpec] = field(default_factory=list)
    profile: NetProfile = field(default_factory=NetProfile)
    trials: int = 5
    timeout_s: float = 900.0
    resume_at_fraction: float = 0.4    # resume kind: kill the client at this share of bytes
    hls_segments: int = 0
    hls_segment_size: int = 0
    public_url: str | None = None
    public_sha256: str | None = None
    idle_seconds: float = 60.0

    @property
    def total_bytes(self) -> int:
        if self.kind == "hls":
            return self.hls_segments * self.hls_segment_size
        return sum(item.size for item in self.files)


def _many(prefix: str, count: int, size: int, seed0: int) -> list[FileSpec]:
    return [FileSpec(f"{prefix}-{index:04d}.bin", size, seed0 + index) for index in range(count)]


def catalogue() -> dict[str, Scenario]:
    items = [
        Scenario("smoke", "http", "Harness self-test: 64 MiB single file plus 10 x 4 MiB.",
                 [FileSpec("smoke-single.bin", 64 * MiB, 1)] + _many("smoke-small", 10, 4 * MiB, 100),
                 trials=2, timeout_s=180),
        Scenario("lan-1g-single", "http", "One 1 GiB file, no shaping: client ceiling on a fast link.",
                 [FileSpec("big-1g.bin", 1024 * MiB, 11)]),
        Scenario("many-100x10m", "http", "100 x 10 MiB files: queueing, per-task overhead, concurrency.",
                 _many("many", 100, 10 * MiB, 1000)),
        Scenario("tiny-1000x64k", "http", "1000 x 64 KiB files: pure per-task overhead (optional).",
                 _many("tiny", 1000, 64 * 1024, 5000), trials=3),
        Scenario("filehost-throttled", "http",
                 "256 MiB; each connection capped at 1 MiB/s, at most 8 concurrent (503 beyond).",
                 [FileSpec("host-256m.bin", 256 * MiB, 21)],
                 NetProfile("filehost-throttled", conn_rate=1 * MiB, max_conns=8)),
        Scenario("filehost-strict", "http",
                 "128 MiB; 1 MiB/s per connection, only 2 connections allowed (503 beyond).",
                 [FileSpec("strict-128m.bin", 128 * MiB, 22)],
                 NetProfile("filehost-strict", conn_rate=1 * MiB, max_conns=2)),
        Scenario("high-latency", "http",
                 "256 MiB with 150 +/- 20 ms per-request delay. Pair with a packet shaper for real RTT.",
                 [FileSpec("latency-256m.bin", 256 * MiB, 31)],
                 NetProfile("high-latency", latency_ms=150, jitter_ms=20)),
        Scenario("flaky-drops", "http", "256 MiB; every response is cut after 16 MiB (tests retry/resume).",
                 [FileSpec("flaky-256m.bin", 256 * MiB, 41)],
                 NetProfile("flaky-drops", drop_after_bytes=16 * MiB)),
        Scenario("no-range", "http", "256 MiB from a server that ignores Range (single stream).",
                 [FileSpec("norange-256m.bin", 256 * MiB, 51)], NetProfile("no-range", ranges=False)),
        Scenario("resume-kill", "resume",
                 "512 MiB at 4 MiB/s per connection; client process tree killed at 40%, then relaunched.",
                 [FileSpec("resume-512m.bin", 512 * MiB, 61)],
                 NetProfile("resume-kill", conn_rate=4 * MiB), timeout_s=1200),
        Scenario("hls-vod", "hls", "Synthetic VOD playlist: 300 x 1 MiB segments (byte-exact concat check).",
                 hls_segments=300, hls_segment_size=1 * MiB),
        Scenario("public-cdn", "public", "A real public file over TLS (set --public-url/--public-sha256).",
                 trials=5, timeout_s=1800),
        Scenario("idle-footprint", "idle", "App launched and idle for 60 s: background CPU and RAM.",
                 trials=3),
    ]
    return {item.name: item for item in items}


def get(name: str, *, public_url: str | None = None, public_sha256: str | None = None,
        public_size: str | None = None) -> Scenario:
    scenarios = catalogue()
    if name not in scenarios:
        raise SystemExit(f"unknown scenario {name!r}; choose from {', '.join(scenarios)}")
    scenario = scenarios[name]
    if scenario.kind == "public":
        if not public_url:
            raise SystemExit("public-cdn needs --public-url (and ideally --public-sha256)")
        leaf = public_url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0] or "public.bin"
        size = parse_size(public_size) if public_size else 0
        scenario.files = [FileSpec(leaf, size, 0)]
        scenario.public_url, scenario.public_sha256 = public_url, public_sha256
    return scenario
