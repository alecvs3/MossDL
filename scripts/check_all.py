from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _redact(value: str) -> str:
    import re
    value = re.sub(r"(?i)(authorization|cookie|token|password|secret|signature)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", value)
    return re.sub(r"https?://[^\s'\"]+", "<url-redacted>", value)


def _run(name: str, command: list[str], timeout: int, temp_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    temp_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"TEMP": str(temp_dir), "TMP": str(temp_dir), "TMPDIR": str(temp_dir)})
    try:
        result = subprocess.run(
            command, cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=timeout
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        output = _redact((stdout + "\n" + stderr).strip())
        return {"name": name, "status": "passed" if result.returncode == 0 else "failed",
                "returncode": result.returncode, "seconds": round(time.monotonic() - started, 2),
                "output_tail": output[-2000:]}
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "failed", "error": "timeout",
                "seconds": round(time.monotonic() - started, 2)}
    except OSError as exc:
        return {"name": name, "status": "environment_error", "error": type(exc).__name__,
                "seconds": round(time.monotonic() - started, 2)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the complete offline Transfer Manager quality gate")
    parser.add_argument("--live", action="store_true", help="run configured provider URLs as well")
    parser.add_argument("-j", "--jobs", type=int, default=min(4, os.cpu_count() or 2),
                        help="concurrent checks (default: min(4, cpu count); pass 1 for serial)")
    parser.add_argument("--profile", choices=("fast", "full"), default="full",
                        help="test runner profile (default: full; pass fast for quick check)")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    python = sys.executable
    npm = "npm.cmd" if os.name == "nt" else "npm"
    jobs = str(min(8, os.cpu_count() or 4))
    checks = [
        ("root-hygiene", [python, "-c", "from pathlib import Path; import sys; root_tests = list(Path('.').glob('test_*.py')); (print(f'Root hygiene error: loose test files in root: {root_tests}'), sys.exit(1)) if root_tests else print('Root is clean')"]),
        ("duplication-check", [npm, "run", "bloat:dups"]),
        ("dead-code-python", [python, "-m", "vulture", "engine/", ".vulture_whitelist", "--min-confidence", "90"]),
        ("contract-scanner", [python, "scripts/contract_scanner.py"]),
        ("dead-code-frontend", [npm, "run", "bloat:dead"]),
        ("python-tests", [python, "scripts/run_all_tests.py", "--profile", args.profile, "-j", jobs]),
        # Phase 25 gate: detection recall/false positives, routing reasons, secrecy, OCR shadow, lifecycle, fairness.
        ("challenge-foundation", [python, "-m", "unittest", "tests.integration.test_challenge_foundation_matrix"]),
        ("provider-health", [python, "scripts/import_cyberdrop_plugins.py", "check"] + (["--live"] if args.live else [])),
        ("tree-picker", [npm, "run", "test:tree"]),
        ("clipboard-intake", [npm, "run", "test:intake"]),
        ("captcha-ui", [npm, "run", "test:captcha"]),
        ("multipart-ui", [npm, "run", "test:multipart"]),
        ("ui-projection", [npm, "run", "test:projection"]),
        ("column-layout", [npm, "run", "test:columnlayout"]),
        ("explore-model", [npm, "run", "test:explore"]),
        ("proxy-parse", [npm, "run", "test:proxies"]),
        ("frontend-build", [npm, "run", "build"]),
        ("rust-check", ["cargo", "check", "--manifest-path", "src-tauri/Cargo.toml"]),
        *([("adblock-live", [python, "-m", "unittest", "tests.live.test_adblock_live"])] if args.live else []),
        # The core's own tests, the WireGuard tunnel's loopback end to end included.
        ("rust-tests", ["cargo", "test", "--manifest-path", "src-tauri/Cargo.toml", "--bin", "transfer-core"]),
    ]
    workers = max(1, min(args.jobs, len(checks)))
    print(f"=== Running {len(checks)} checks with {workers} worker(s) ===", flush=True)
    run_started = time.monotonic()
    results_by_name: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="transfer-quality-gate-") as temp_root_str:
        temp_root = Path(temp_root_str)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run, name, command, args.timeout, temp_root / name): name
                for name, command in checks
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"name": name, "status": "failed", "seconds": 0.0,
                              "error": f"runner error: {type(exc).__name__}: {exc}"}
                results_by_name[name] = result
                marker = "PASSED" if result["status"] == "passed" else "FAILED"
                print(f"{marker}: {name} ({result['seconds']}s)", flush=True)
    results = [results_by_name[name] for name, _command in checks]
    report = {"schema": 1, "root": str(ROOT), "results": results,
              "summary": {"checks": len(results),
                          "passed": sum(item["status"] == "passed" for item in results),
                          "failed": sum(item["status"] != "passed" for item in results),
                          "seconds": round(time.monotonic() - run_started, 2)}}
    output = ROOT / ".test-artifacts" / "quality-gate.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for item in results:
        if item["status"] != "passed":
            print(f"--- {item['name']} output tail ---", flush=True)
            print(item.get("output_tail") or item.get("error") or "(no output)", flush=True)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
