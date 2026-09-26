"""Run the deterministic transfer-core benchmark matrix."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.benchmarking import (  # noqa: E402
    CORE_SCENARIOS,
    FIXTURE_CASES,
    BenchmarkRunner,
    human_summary,
    write_report,
)


class TerminalProgressBar:
    """Render a live ASCII/ANSI progress bar during benchmark transfers."""

    def __init__(self, stream=None, width: int = 24) -> None:
        self.stream = stream or sys.stdout
        self.width = width
        self._current_key: tuple[str, str] | None = None
        self._started_at: float = 0.0
        self._last_rendered: float = 0.0
        self._finished_keys: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        try:
            "█░".encode(getattr(self.stream, "encoding", "utf-8") or "utf-8")
            self._use_unicode = True
        except (UnicodeEncodeError, LookupError):
            self._use_unicode = False
        if os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                pass

    def __call__(self, backend: str, scenario: str, current_bytes: int, total_bytes: int) -> None:
        try:
            with self._lock:
                now = time.monotonic()
                key = (backend, scenario)
                if self._current_key != key:
                    if self._current_key is not None and self._current_key not in self._finished_keys:
                        self.stream.write("\n")
                        self.stream.flush()
                    self._current_key = key
                    self._started_at = now
                    self._last_rendered = 0.0

                if key in self._finished_keys:
                    return

                is_done = total_bytes > 0 and current_bytes >= total_bytes
                # Rate limit updates on active transfers to ~25 fps to prevent console flickering
                if not is_done and (now - self._last_rendered) < 0.04:
                    return
                self._last_rendered = now

                elapsed = max(0.000001, now - self._started_at)
                rate = (current_bytes / elapsed) if elapsed >= 0.05 else 0.0
                rate_mb = rate / (1024 * 1024)
                rate_str = f"{rate_mb:6.1f} MB/s" if elapsed >= 0.05 else "  --.- MB/s"

                if total_bytes > 0:
                    fraction = min(1.0, max(0.0, current_bytes / total_bytes))
                    percent = fraction * 100.0
                    filled = int(round(self.width * fraction))
                    filled = min(self.width, max(0, filled))
                    bar = ("█" * filled + "░" * (self.width - filled)) if self._use_unicode else ("=" * filled + "-" * (self.width - filled))
                    remaining_bytes = max(0, total_bytes - current_bytes)
                    eta_sec = remaining_bytes / rate if rate > 0 else 0.0
                    if is_done:
                        eta_str = f"in {elapsed:.2f}s"
                    elif rate > 0 and eta_sec < 60:
                        eta_str = f"ETA {eta_sec:.1f}s"
                    elif rate > 0:
                        eta_str = f"ETA {int(eta_sec // 60)}m{int(eta_sec % 60):02d}s"
                    else:
                        eta_str = "ETA --.-s"
                    line = (
                        f"\r[{bar}] {percent:5.1f}% | {rate_str.strip()} | {eta_str} | ({backend} / {scenario})"
                    )
                else:
                    cur_mb = current_bytes / (1024 * 1024)
                    line = f"\r[{backend} / {scenario}] {cur_mb:.1f} MB | {rate_str.strip()}"

                padded_line = line.ljust(85)
                self.stream.write(padded_line)
                if is_done:
                    self._finished_keys.add(key)
                    self.stream.write("\n")
                self.stream.flush()
        except Exception:
            pass

    def finish(self) -> None:
        try:
            with self._lock:
                if self._current_key is not None and self._current_key not in self._finished_keys:
                    self.stream.write("\n")
                    self.stream.flush()
                    self._finished_keys.add(self._current_key)
        except Exception:
            pass


def print_comparison_diff(current_report: Any, baseline_path: str | Path) -> None:
    path = Path(baseline_path)
    if not path.is_file():
        print(f"Warning: Baseline file not found at {path}")
        return
    try:
        content = path.read_text(encoding="utf-8")
        if path.suffix == ".jsonl":
            baseline_entries = [json.loads(line) for line in content.splitlines() if line.strip()]
        else:
            baseline_data = json.loads(content)
            baseline_entries = baseline_data.get("results", [])
    except Exception as exc:
        print(f"Warning: Could not parse baseline report {path}: {exc}")
        return

    baseline_map: dict[tuple[str, str], float] = {}
    for entry in baseline_entries:
        key = (str(entry.get("backend", "")), str(entry.get("scenario", "")))
        rate = float(entry.get("metrics", {}).get("throughput_bytes_per_second", 0.0) or
                     entry.get("metrics", {}).get("wire_throughput_bytes_per_second", 0.0))
        baseline_map[key] = rate

    print("\n" + "=" * 80)
    print("                      ACCELERATION BENCHMARK COMPARISON")
    print("=" * 80)
    print(f"{'Scenario':<20} {'Backend':<10} {'Baseline':<14} {'Accelerated':<14} {'Speedup':<9} {'Status'}")
    print("-" * 80)
    for res in current_report.results:
        key = (res.backend, res.scenario)
        accel_rate = float(res.metrics.get("throughput_bytes_per_second", 0.0) or
                           res.metrics.get("wire_throughput_bytes_per_second", 0.0))
        base_rate = baseline_map.get(key, 0.0)
        accel_mb = accel_rate / (1024 * 1024)
        base_mb = base_rate / (1024 * 1024)
        if base_rate > 0:
            speedup = accel_rate / base_rate
            speedup_str = f"{speedup:6.1f}x"
        else:
            speedup_str = "   N/A  "
        status = "PASSED" if res.gate.get("passed") else "FAILED"
        print(f"{res.scenario:<20} {res.backend:<10} {base_mb:6.1f} MB/s     {accel_mb:6.1f} MB/s     {speedup_str:<9} {status}")
    print("=" * 80 + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic custom/Rust/aria2 transfer benchmarks")
    parser.add_argument("--scenario", default="all",
                        choices=("all", *CORE_SCENARIOS, *FIXTURE_CASES),
                        help="core scenario or fixture behavior case")
    parser.add_argument("--output", "--json", dest="output", default=None,
                        help="JSON or JSONL report path; .jsonl selects JSONL")
    parser.add_argument("--jsonl", action="store_true", help="write one normalized result per line")
    parser.add_argument("--summary", action="store_true", help="print the concise human summary")
    parser.add_argument("--no-summary", action="store_true", help="suppress the human summary")
    parser.add_argument("--uncapped", action="store_true",
                        help="run benchmarks with uncapped fixture rate (0 delay)")
    parser.add_argument("--no-progress", action="store_true",
                        help="disable live progress bar")
    parser.add_argument("--diff", default=None,
                        help="compare results against a baseline report JSON/JSONL")
    parser.add_argument("--no-threaded", action="store_true",
                        help="disable concurrent scenario execution")
    parser.add_argument("--authorized-live-url", "--live-url", dest="live_url",
                        help="explicitly probe one authorized live target as non-gating evidence")
    parser.add_argument("--authorize-live", action="store_true",
                        help="required acknowledgement for --authorized-live-url")
    parser.add_argument("--output-dir", default=None, help="temporary fixture artifact directory")
    args = parser.parse_args(argv)
    if args.live_url and not args.authorize_live:
        parser.error("--authorized-live-url requires --authorize-live")
    progress_bar = None if args.no_progress else TerminalProgressBar()
    runner = BenchmarkRunner(args.output_dir, uncapped=args.uncapped, progress_callback=progress_bar)
    try:
        report = runner.run(
            args.scenario,
            authorized_live_url=args.live_url,
            authorize_live=args.authorize_live,
            threaded=not args.no_threaded,
        )
    finally:
        if progress_bar:
            progress_bar.finish()
    if args.output:
        write_report(report, args.output, jsonl=True if args.jsonl else None)
    if args.summary or (not args.no_summary and not args.output):
        print(human_summary(report))
    if args.diff:
        print_comparison_diff(report, args.diff)
    data = report.to_dict()
    return 0 if data["summary"]["all_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
