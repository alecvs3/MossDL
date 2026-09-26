"""Benchmark runner: scenarios x clients x trials, interleaved and randomized.

Examples (run from docs/benchmarks):
  python -m harness.runner --scenario smoke --client mossdl
  python -m harness.runner --scenario lan-1g-single --scenario many-100x10m \
      --client mossdl --client motrix --client jdownloader --policy both \
      --opt jdownloader.jd_home="C:/Users/me/AppData/Local/JDownloader 2.0" --opt jdownloader.confirmed=1
  python -m harness.runner --scenario public-cdn --client mossdl \
      --public-url https://example.org/file.iso --public-sha256 <hex> --public-size 3303444480

Every trial appends one record to results/<run_id>.jsonl; the environment is
saved once to results/<run_id>.env.json. Aggregate with harness.aggregate.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import tempfile
import time
from pathlib import Path

from . import adapters, env, schema, scenarios, trial
from .adapters.base import Unavailable
from .server import BenchServer

RESULTS = Path(__file__).resolve().parents[1] / "results"


def parse_options(pairs: list[str]) -> dict[str, dict]:
    options: dict[str, dict] = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        client, _, name = key.partition(".")
        if not name:
            raise SystemExit(f"--opt expects client.key=value, got {pair!r}")
        options.setdefault(client, {})[name] = value
    return options


def client_info(adapter, job_or_none, policy: str) -> dict:
    return {"name": adapter.display_name, "adapter": adapter.name, "version": adapter.version(),
            "mode": getattr(adapter, "mode", None), "policy": policy, "automation": adapter.automation,
            "settings": adapter.applied_settings(job_or_none) if job_or_none else {}}


def run_scenario(scenario, args, clients: dict, out: Path, run_id: str, rng: random.Random) -> None:
    server = None
    base_url = args.server_url
    if scenario.kind in ("http", "hls", "resume") and not base_url:
        server = BenchServer("127.0.0.1", 0, scenario.profile).start_background()
        base_url = server.base_url
    link = trial.ServerLink(base_url) if base_url and scenario.kind != "public" else None
    policies = ["defaults", "equalized"] if args.policy == "both" else [args.policy]
    trials = args.trials or scenario.trials
    try:
        for policy in policies:
            for round_index in range(args.warmup + trials):
                order = list(clients.items())
                rng.shuffle(order)  # interleave clients so drift/time-of-day hits all of them
                for name, adapter in order:
                    dest = Path(args.work_dir) / run_id / f"{scenario.name}-{name}-{policy}-{round_index}" / "dl"
                    job = trial.build_job(scenario, base_url, dest, policy, args.connections, args.concurrent)
                    record = schema.new_record(run_id, scenario, client_info(adapter, job, policy),
                                               trial=max(0, round_index - args.warmup), round_index=round_index,
                                               warmup=round_index < args.warmup)
                    reason = getattr(adapter, "_unavailable", None) or adapter.unsupported_reason(job)
                    if reason:
                        if round_index == 0:  # one 'unsupported' record per client/scenario/policy
                            record.update(status="unsupported", error=reason)
                            schema.append(out, schema.finalize(record))
                            report(record)
                        continue
                    trial.run(adapter, scenario, job, record, link, args.sample_interval)
                    try:
                        adapter.cleanup()
                    except Exception as exc:
                        record["notes"].append(f"cleanup: {type(exc).__name__}: {exc}"[:200])
                    schema.append(out, schema.finalize(record))
                    report(record)
                    if record["status"] == "ok" or not args.keep_failed:
                        shutil.rmtree(dest.parent, ignore_errors=True)
    finally:
        if server:
            server.stop()


def report(record: dict) -> None:
    rate = record["throughput"]["goodput_bytes_per_s"]
    res = record.get("resources") or {}
    print(f"[{record['scenario']['name']}] {record['client']['name']:<24} {record['client']['policy']:<9} "
          f"round {record['round']}{' (warm-up)' if record['warmup'] else ''}: {record['status']}"
          + (f"  {rate / 2**20:8.1f} MiB/s  total {record['timing']['total_s']:.2f}s" if rate else "")
          + (f"  cpu {res.get('cpu_seconds', 0):.1f}s  peak {res.get('peak_tree_wset_bytes', 0) / 2**20:.0f} MiB" if res else "")
          + (f"  -- {record['error']}" if record.get("error") else ""), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", action="append", required=True)
    parser.add_argument("--client", action="append", required=True, choices=sorted(adapters.REGISTRY))
    parser.add_argument("--policy", choices=("defaults", "equalized", "both"), default="defaults")
    parser.add_argument("--connections", type=int, default=8, help="per-file connections under 'equalized'")
    parser.add_argument("--concurrent", type=int, default=4, help="concurrent downloads under 'equalized'")
    parser.add_argument("--trials", type=int, default=0, help="override the scenario's trial count")
    parser.add_argument("--warmup", type=int, default=1, help="discarded warm-up rounds per policy")
    parser.add_argument("--server-url", default=None, help="use an external harness server (LAN runs)")
    parser.add_argument("--work-dir", default=str(Path(tempfile.gettempdir()) / "mossdl-bench"))
    parser.add_argument("--out-dir", default=str(RESULTS))
    parser.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--opt", action="append", default=[], help="client.key=value adapter option")
    parser.add_argument("--public-url"), parser.add_argument("--public-sha256"), parser.add_argument("--public-size")
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=20260925, help="client-order shuffle seed")
    parser.add_argument("--keep-failed", action="store_true", help="keep downloaded files of failed trials")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.run_id}.jsonl"
    options = parse_options(args.opt)
    clients = {}
    for name in dict.fromkeys(args.client):
        adapter = adapters.create(name, options.get(name))
        try:
            # Never drive an installed GUI app until the operator has configured it
            # (METHODOLOGY section 5) and said so.
            if name not in ("mossdl", "aria2") and not adapter.options.get("confirmed"):
                raise Unavailable(f"{adapter.display_name}: pass --opt {name}.confirmed=1 after configuring it")
            adapter.check()
        except Unavailable as exc:
            adapter._unavailable = str(exc)  # recorded as 'unsupported' in every scenario
            print(f"[{name}] unavailable: {exc}")
        clients[name] = adapter
    environment = env.capture(Path(args.work_dir) if Path(args.work_dir).exists() else Path(tempfile.gettempdir()))
    environment["argv"] = vars(args)
    (out_dir / f"{args.run_id}.env.json").write_text(json.dumps(environment, indent=2, default=str), encoding="utf-8")
    rng = random.Random(args.seed)
    for name in args.scenario:
        scenario = scenarios.get(name, public_url=args.public_url, public_sha256=args.public_sha256,
                                 public_size=args.public_size)
        run_scenario(scenario, args, clients, out, args.run_id, rng)
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
