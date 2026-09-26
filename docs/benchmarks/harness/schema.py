"""Result record construction and light validation.

One JSON object per trial, appended to ``results/<run_id>.jsonl``. The formal
JSON Schema lives in ``result.schema.json`` next to this file; ``validate``
checks the required structure without third-party packages.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from . import SCHEMA_VERSION

STATUSES = ("ok", "failed", "timeout", "unsupported", "error")
SCHEMA_PATH = Path(__file__).with_name("result.schema.json")


def new_record(run_id: str, scenario, client: dict, *, trial: int, round_index: int, warmup: bool) -> dict:
    """Skeleton record; every measured field starts as None (never a guess)."""
    return {
        "schema": SCHEMA_VERSION,
        "record_id": uuid.uuid4().hex,
        "run_id": run_id,
        "trial": trial,
        "round": round_index,
        "warmup": warmup,
        "scenario": {
            "name": scenario.name,
            "kind": scenario.kind,
            "profile": scenario.profile.to_dict(),
            "total_bytes": scenario.total_bytes,
            "file_count": len(scenario.files) if scenario.kind != "hls" else 1,
            "source": "public" if scenario.kind == "public" else "harness-server",
        },
        "client": client,
        "timing": {"submitted_at": None, "first_byte_at": None, "last_byte_at": None,
                   "completed_at": None, "total_s": None, "ttfb_s": None, "wire_s": None,
                   "killed_at": None, "relaunched_at": None},
        "throughput": {"goodput_bytes_per_s": None, "wire_bytes_per_s": None},
        "resources": None,
        "server": None,
        "correctness": {"files_total": None, "files_ok": None, "all_ok": None, "problems": []},
        "status": "error",
        "error": None,
        "notes": [],
    }


def finalize(record: dict) -> dict:
    """Derive durations/rates from recorded timestamps. Missing inputs stay None."""
    timing = record["timing"]
    submitted, completed = timing["submitted_at"], timing["completed_at"]
    first, last = timing["first_byte_at"], timing["last_byte_at"]
    if submitted is not None and completed is not None:
        timing["total_s"] = max(0.0, completed - submitted)
    if submitted is not None and first is not None:
        timing["ttfb_s"] = max(0.0, first - submitted)
    if first is not None and last is not None:
        timing["wire_s"] = max(0.0, last - first)
    total = record["scenario"]["total_bytes"]
    if record["status"] == "ok" and total:
        if timing["total_s"]:
            record["throughput"]["goodput_bytes_per_s"] = total / timing["total_s"]
        if timing["wire_s"]:
            record["throughput"]["wire_bytes_per_s"] = total / timing["wire_s"]
    return record


def validate(record: dict) -> list[str]:
    problems = []
    for key in ("schema", "run_id", "scenario", "client", "timing", "throughput", "correctness", "status"):
        if key not in record:
            problems.append(f"missing {key}")
    if record.get("status") not in STATUSES:
        problems.append(f"bad status {record.get('status')!r}")
    if record.get("status") == "ok" and not (record.get("correctness") or {}).get("all_ok"):
        problems.append("status ok requires correctness.all_ok")
    for key in ("name", "adapter", "policy"):
        if key not in (record.get("client") or {}):
            problems.append(f"client.{key} missing")
    return problems


def append(path: Path, record: dict) -> None:
    problems = validate(record)
    if problems:
        record.setdefault("notes", []).append("schema problems: " + "; ".join(problems))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def load(paths) -> list[dict]:
    records = []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records
