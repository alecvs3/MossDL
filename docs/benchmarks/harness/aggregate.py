"""Aggregate trial records into a summary CSV and a Markdown table.

    python -m harness.aggregate results/20260925-*.jsonl --csv results/summary.csv --md results/summary.md

Rules: warm-up trials are excluded; only status == ok trials feed the
statistics, and the success count (n_ok/n) is always shown next to them; a
metric nobody measured stays blank. Medians come with the interquartile
range and a seeded percentile-bootstrap 95% CI of the median.
"""

from __future__ import annotations

import argparse
import csv
import glob
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from . import schema

MiB = 1024 * 1024
METRICS = {
    "goodput_mib_s": lambda r: _div(r["throughput"]["goodput_bytes_per_s"], MiB),
    "wire_mib_s": lambda r: _div(r["throughput"]["wire_bytes_per_s"], MiB),
    "total_s": lambda r: r["timing"]["total_s"],
    "ttfb_s": lambda r: r["timing"]["ttfb_s"],
    "cpu_s": lambda r: (r.get("resources") or {}).get("cpu_seconds"),
    "cpu_s_per_gib": lambda r: _per_gib((r.get("resources") or {}).get("cpu_seconds"), r),
    "peak_wset_mib": lambda r: _div((r.get("resources") or {}).get("peak_tree_wset_bytes"), MiB),
    "peak_private_mib": lambda r: _div((r.get("resources") or {}).get("peak_tree_private_bytes"), MiB),
    "disk_write_amplification": lambda r: _ratio((r.get("resources") or {}).get("disk_write_bytes"), r),
    "server_peak_connections": lambda r: (r.get("server") or {}).get("peak_active"),
    "server_overhead_ratio": lambda r: (r.get("server") or {}).get("overhead_ratio"),
}


def _div(value, by):
    return None if value is None else value / by


def _per_gib(value, record):
    total = record["scenario"]["total_bytes"]
    return None if value is None or not total else value / (total / (1024 * MiB))


def _ratio(value, record):
    total = record["scenario"]["total_bytes"]
    return None if value is None or not total else value / total


def quartiles(values: list[float]) -> tuple[float, float, float]:
    if len(values) == 1:
        return values[0], values[0], values[0]
    q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, q2, q3


def bootstrap_median_ci(values: list[float], resamples: int = 2000, seed: int = 1) -> tuple[float, float] | None:
    if len(values) < 3:
        return None  # too few points for an interval worth printing
    rng = random.Random(seed)
    medians = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(resamples))
    return medians[int(0.025 * resamples)], medians[int(0.975 * resamples) - 1]


def summarize(records: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for record in records:
        if record.get("warmup"):
            continue
        key = (record["scenario"]["name"], record["client"]["name"], record["client"]["policy"],
               record["client"].get("mode") or "")
        groups[key].append(record)
    rows = []
    for (scenario, client, policy, mode), items in sorted(groups.items()):
        ok = [item for item in items if item["status"] == "ok"]
        statuses = defaultdict(int)
        for item in items:
            statuses[item["status"]] += 1
        row = {"scenario": scenario, "client": client, "policy": policy, "mode": mode,
               "version": items[-1]["client"].get("version"), "n": len(items), "n_ok": len(ok),
               "statuses": " ".join(f"{k}={v}" for k, v in sorted(statuses.items())),
               "errors": " | ".join(sorted({str(i.get("error")) for i in items if i.get("error")}))[:300]}
        for metric, getter in METRICS.items():
            values = [v for v in (getter(item) for item in ok) if v is not None]
            if not values:
                continue
            q1, median, q3 = quartiles(values)
            row.update({f"{metric}_median": median, f"{metric}_q1": q1, f"{metric}_q3": q3,
                        f"{metric}_min": min(values), f"{metric}_max": max(values)})
            ci = bootstrap_median_ci(values)
            if ci:
                row[f"{metric}_ci95_lo"], row[f"{metric}_ci95_hi"] = ci
        rows.append(row)
    return rows


def markdown(rows: list[dict]) -> str:
    def cell(row, metric, digits=1):
        if f"{metric}_median" not in row:
            return ""
        return f"{row[f'{metric}_median']:.{digits}f} ({row[f'{metric}_q1']:.{digits}f}-{row[f'{metric}_q3']:.{digits}f})"
    lines = ["| Scenario | Client | Policy | ok/n | Goodput MiB/s | Total s | TTFB s | CPU s | Peak RAM MiB | Server conns |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['scenario']} | {row['client']}{' [' + row['mode'] + ']' if row['mode'] else ''} | "
                     f"{row['policy']} | {row['n_ok']}/{row['n']} | {cell(row, 'goodput_mib_s')} | "
                     f"{cell(row, 'total_s', 2)} | {cell(row, 'ttfb_s', 3)} | {cell(row, 'cpu_s', 2)} | "
                     f"{cell(row, 'peak_wset_mib', 0)} | {cell(row, 'server_peak_connections', 0)} |")
    lines.append("")
    lines.append("Cells: median (IQR). Only successful, non-warm-up trials are summarised; see ok/n.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", help="results/*.jsonl")
    parser.add_argument("--csv", default=None)
    parser.add_argument("--md", default=None)
    args = parser.parse_args(argv)
    paths = [Path(path) for pattern in args.inputs for path in (sorted(glob.glob(pattern)) or [pattern])]
    rows = summarize(schema.load([p for p in paths if not p.name.endswith(".env.json")]))
    if not rows:
        print("no records", file=sys.stderr)
        return 1
    if args.csv:
        fields = sorted({key for row in rows for key in row}, key=lambda k: (k not in rows[0], k))
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    table = markdown(rows)
    if args.md:
        Path(args.md).write_text(table + "\n", encoding="utf-8")
    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
