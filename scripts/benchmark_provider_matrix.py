"""Run cold, warm, and challenge provider-resolution profiles and aggregate them."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append")
    parser.add_argument("--urls-file", type=Path)
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT / "artifacts" / "benchmarks" / "provider-resolution")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()
    if not args.url and not args.urls_file:
        parser.error("provide --url or --urls-file")

    summaries: list[dict] = []
    exit_code = 0
    for profile in ("cold", "warm", "challenge"):
        command = [
            sys.executable, str(ROOT / "scripts" / "benchmark_provider_resolution.py"),
            "--profile", profile,
            "--repeat", str(args.repeat),
            "--timeout", str(args.timeout),
            "--output-dir", str(args.output_dir),
        ]
        for url in args.url or []:
            command.extend(("--url", url))
        if args.urls_file:
            command.extend(("--urls-file", str(args.urls_file)))
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        if completed.returncode:
            exit_code = completed.returncode
        try:
            summaries.append(json.loads(completed.stdout))
        except json.JSONDecodeError:
            summaries.append({
                "profile": profile, "error": "benchmark emitted non-JSON output",
                "stdout_tail": completed.stdout[-1000:], "stderr_tail": completed.stderr[-1000:],
            })

    matrix = {
        "schema": 1,
        "repeat_per_profile": args.repeat,
        "profiles": summaries,
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = args.output_dir / f"{stamp}-provider-resolution-matrix.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(matrix, indent=2), encoding="utf-8")
    matrix["matrix_path"] = str(path.resolve())
    print(json.dumps(matrix, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
