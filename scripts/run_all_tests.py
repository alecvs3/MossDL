"""Test runner executing test suites in isolated processes to prevent DLL/heap collision.

Suites and root test modules are independent processes with no shared fixed
ports or state, so they can run concurrently. Each job receives a private
TEMP/TMP directory to keep temp-file collisions out of the picture entirely.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMINGS_PATH = ROOT / ".test-artifacts" / "test-timings.json"

SUITE_DIRS = (
    "tests/unit", "tests/integration", "tests/browser_capture",
    "tests/browser_extension", "tests/crawling", "tests/e2e",
)
FAST_DIRS = ("tests/unit", "tests/integration")
RESOURCE_INTENSIVE = {
    "tests/integration/test_cyberdrop_import.py",
    "tests/integration/test_high_performance_downloader.py",
    "tests/integration/test_multipart_download_hardening.py",
    "tests/integration/test_transfer_benchmark.py",
    "tests/test_direct_to_disk.py",
    "tests/test_release_hardening.py",
    "tests/test_segment_stealer.py",
    "tests/unit/test_custom_engine.py",
    "tests/unit/test_datanodes_multipart_fix.py",
    "tests/unit/test_vikingfile_turnstile_fix.py",
}


def build_jobs(*, profile: str = "full", granularity: str = "module") -> list[tuple[str, list[str]]]:
    suite_dirs = FAST_DIRS if profile == "fast" else SUITE_DIRS
    jobs: list[tuple[str, list[str]]] = []
    for suite in suite_dirs:
        suite_path = ROOT / suite
        if granularity == "suite":
            jobs.append((suite, [
                sys.executable, "-m", "unittest", "discover",
                "-s", suite, "-p", "test_*.py",
            ]))
            continue
        for test_path in sorted(suite_path.glob("test_*.py")):
            relative = test_path.relative_to(ROOT).as_posix()
            jobs.append((relative, [sys.executable, "-m", "unittest", relative]))
    for test_path in sorted((ROOT / "tests").glob("test_*.py")):
        relative = test_path.relative_to(ROOT).as_posix()
        jobs.append((relative, [sys.executable, "-m", "unittest", relative]))
    try:
        timings = json.loads(TIMINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        timings = {}
    if granularity == "module":
        # Longest-processing-time-first keeps known benchmark modules from
        # becoming a serial tail after dozens of sub-second unit modules.
        jobs.sort(key=lambda job: (-float(timings.get(job[0], 0.0)), job[0]))
    return jobs


def _save_timings(results: dict[str, dict]) -> None:
    try:
        prior = json.loads(TIMINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        prior = {}
    for name, result in results.items():
        if result.get("seconds") is not None:
            prior[name] = float(result["seconds"])
    TIMINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = TIMINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(prior, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(TIMINGS_PATH)


def _job_temp_dir(temp_root: Path, name: str) -> Path:
    safe_name = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
    temp_dir = temp_root / safe_name
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def run_job(name: str, command: list[str], timeout: int, temp_root: Path,
            heavy_gate: threading.Semaphore) -> dict:
    temp_dir = _job_temp_dir(temp_root, name)
    env = os.environ.copy()
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(temp_dir)
    started = time.monotonic()
    gate = heavy_gate if name in RESOURCE_INTENSIVE else nullcontext()
    try:
        with gate:
            started = time.monotonic()
            result = subprocess.run(
                command, cwd=str(ROOT), env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "failed",
                "seconds": round(time.monotonic() - started, 2),
                "output": f"timeout after {timeout}s"}
    return {
        "name": name,
        "status": "passed" if result.returncode == 0 else "failed",
        "seconds": round(time.monotonic() - started, 2),
        "output": (result.stderr or result.stdout or "").strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the complete Python test suite")
    parser.add_argument(
        "-j", "--jobs", type=int, default=min(8, os.cpu_count() or 2),
        help="concurrent test processes (default: min(8, cpu count); pass 1 for serial)",
    )
    parser.add_argument("--profile", choices=("fast", "full"), default="full",
                        help="fast runs unit/integration/root tests; full adds browser, crawling, and e2e")
    parser.add_argument("--granularity", choices=("module", "suite"), default="module",
                        help="module schedules files dynamically; suite preserves the old coarse runner")
    parser.add_argument("--heavy-jobs", type=int, default=8,
                        help="maximum simultaneous native/benchmark modules (default: 8)")
    parser.add_argument("--timeout", type=int, default=1800, help="per-job timeout in seconds")
    args = parser.parse_args()

    jobs = build_jobs(profile=args.profile, granularity=args.granularity)
    if not jobs:
        print("No test jobs found", flush=True)
        return 1
    workers = max(1, min(args.jobs, len(jobs)))
    failed = False
    results_by_name: dict[str, dict] = {}
    run_started = time.monotonic()
    heavy_gate = threading.Semaphore(max(1, min(args.heavy_jobs, workers)))
    with tempfile.TemporaryDirectory(prefix="transfer-tests-") as temp_root_str:
        temp_root = Path(temp_root_str)
        print(f"=== Running {len(jobs)} test jobs with {workers} worker(s) ===", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(run_job, name, command, args.timeout, temp_root, heavy_gate): name
                for name, command in jobs
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"name": name, "status": "failed", "seconds": 0.0,
                              "output": f"runner error: {type(exc).__name__}: {exc}"}
                results_by_name[name] = result
                if result["status"] == "passed":
                    print(f"PASSED: {result['name']} ({result['seconds']}s)", flush=True)
                else:
                    failed = True
                    print(f"FAILED: {result['name']} ({result['seconds']}s)", flush=True)
                    if result.get("output"):
                        print(result["output"], flush=True)
    _save_timings(results_by_name)
    # Repeat failures last: callers such as check_all keep only the output tail,
    # and an intermittent failure must name itself there.
    for result in results_by_name.values():
        if result["status"] != "passed":
            print(f"=== FAILED {result['name']} ===\n{(result.get('output') or '')[-1500:]}", flush=True)
    print(f"=== Finished in {round(time.monotonic() - run_started, 2)}s ===", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
