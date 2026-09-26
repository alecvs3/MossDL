"""Replay captured engine run artifacts into UI projection snapshots.

The cardinal rule (agents.md) is that the engine is the sole source of truth for
download state.  ``tests/ui_projection.test.ts`` proves the UI projection never
contradicts that truth -- but only if it is fed states the engine *actually*
produced.  This module turns already-captured run artifacts
(``summary.json`` + ``telemetry.jsonl``) into a deterministic, offline
``*.ui_snapshots.jsonl`` fixture: one snapshot of the engine's whole task table
per observable transition.

Nothing here interprets state for the UI.  It only *reconstructs* the engine's
own task rows (the same shape ``list_tasks`` returns) by replaying the telemetry
bus.  Every field written is copied from a telemetry record or from
``summary.json``; values the artifacts do not record are omitted rather than
invented, and each snapshot carries an ``engine_truth`` block holding the
independently-derived ground truth the TypeScript test asserts against.

Usage::

    python tests/live/ui_snapshots.py                      # regenerate all fixtures
    python tests/live/ui_snapshots.py --source <dir> ...   # specific run dirs
    python tests/live/ui_snapshots.py --check              # fail if fixtures are stale
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "ui_snapshots"

# Captured runs replayed by default.  Each must contain summary.json +
# telemetry.jsonl.  Live runs come first so a fixture refresh keeps the real
# multi-gigabyte DataNodes runs as the primary evidence.
DEFAULT_SOURCES = [
    "reports/live/20260917_031249_Resonance",
    "reports/live/20260916_231134_Kristala",
    ".test-artifacts/lifecycle-matrix/ab_shared_clearance",
    ".test-artifacts/lifecycle-matrix/multipart_shared_captcha",
    ".test-artifacts/lifecycle-matrix/ab_per_part_solve",
    ".test-artifacts/lifecycle-matrix/single_file_timer_captcha",
    ".test-artifacts/lifecycle-matrix/token_rejected_once",
    ".test-artifacts/lifecycle-matrix/solver_timeout",
    ".test-artifacts/lifecycle-matrix/checksum_mismatch",
]

STATE_RE = re.compile(r"\(state\) Task state changed to (\w+)")
TIMER_TICK_RE = re.compile(r"\[TIMER_TICK\].*?(\d+)s remaining", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s'\"<>]+")
PART_RE = re.compile(r"\.part(\d+)\.(rar|r\d+|zip|7z)", re.IGNORECASE)


def _parse_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _leaf(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0].rstrip("/").split("/")[-1]


class RunReplay:
    """Rebuilds the engine task table from one captured run's telemetry."""

    def __init__(self, source: Path, slug: str) -> None:
        self.source = source
        self.slug = slug
        self.summary: dict[str, Any] = json.loads((source / "summary.json").read_text(encoding="utf-8"))
        self.records = self._load_telemetry(source / "telemetry.jsonl")

        self.tasks: dict[str, dict[str, Any]] = {}
        self.challenges: dict[str, dict[str, Any]] = {}
        self.archive: dict[str, Any] | None = None
        self.clock: float = 0.0
        self.snapshots: list[dict[str, Any]] = []
        # Tasks whose completed_bytes the artifacts actually pin down.
        self.bytes_known: set[str] = set()
        self.seq = 0
        # Engine-reported countdown observations: (epoch_seconds, task_id, remaining)
        self.timer_observations: list[dict[str, Any]] = []

        self.url_by_leaf: dict[str, str] = {}
        self.name_by_task: dict[str, str] = {}
        self._index_identities()

    # ---------------------------------------------------------------- loading

    @staticmethod
    def _load_telemetry(path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def _index_identities(self) -> None:
        """Harvest real file names / URLs from the artifacts (never synthesized)."""
        package_urls = ((self.summary.get("package") or {}).get("urls") or {})
        for url in package_urls.values():
            if isinstance(url, str):
                self.url_by_leaf[_leaf(url)] = url
        for record in self.records:
            context = record.get("context") or {}
            message = record.get("message") or ""
            for candidate in list(context.values()) + [message]:
                if not isinstance(candidate, str):
                    continue
                for url in URL_RE.findall(candidate):
                    leaf = _leaf(url)
                    if PART_RE.search(leaf) or leaf.lower().endswith((".rar", ".zip", ".7z", ".bin")):
                        self.url_by_leaf.setdefault(leaf, url)
            task_id = context.get("task_id")
            name = context.get("display_name")
            if task_id and isinstance(name, str) and name:
                self.name_by_task.setdefault(task_id, name)
            if task_id and not self.name_by_task.get(task_id):
                head = message.split("]", 1)[-1].strip()
                leaf_match = re.match(r"([^\s:]+\.(?:part\d+\.)?(?:rar|zip|7z|bin))", head, re.IGNORECASE)
                if leaf_match:
                    self.name_by_task.setdefault(task_id, leaf_match.group(1))

    # ------------------------------------------------------------ task fields

    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is not None:
            return task
        package = (self.summary.get("task_packages") or {}).get(task_id) or {}
        name = self.name_by_task.get(task_id)
        if not name:
            part = package.get("part")
            if part is not None:
                for leaf in self.url_by_leaf:
                    match = PART_RE.search(leaf)
                    if match and int(match.group(1)) == int(part):
                        name = leaf
                        break
        url = self.url_by_leaf.get(name or "", "")
        task = {
            "id": task_id,
            "source_url": url,
            "display_name": name,
            "destination": self._destination(),
            "state": "queued",
            "completed_bytes": 0,
            "stage_detail": {},
        }
        size = package.get("size")
        if isinstance(size, int):
            task["size"] = size
        if package.get("package_key"):
            task["package_key"] = package["package_key"]
        if package.get("part") is not None:
            task["package_part_number"] = package["part"]
        if package.get("leader"):
            task["package_leader_id"] = package["leader"]
        self.tasks[task_id] = task
        return task

    def _destination(self) -> str:
        jobs = self.summary.get("archive_jobs") or []
        for job in jobs:
            directory = job.get("output_directory")
            if directory:
                return directory
        return ""

    # -------------------------------------------------------------- snapshots

    def _emit(self, trigger: str, timestamp: str) -> None:
        tasks = [self._public_task(task) for task in self.tasks.values()]
        pending = [dict(challenge) for challenge in self.challenges.values()]
        snapshot = {
            "run": self.slug,
            "seq": self.seq,
            "at": round(self.clock, 6),
            "timestamp": timestamp,
            "trigger": trigger,
            "tasks": tasks,
            "captcha_pending": pending,
            "engine_truth": {
                "open_challenge_ids": sorted(self.challenges),
                "needs_user_task_ids": sorted(t["id"] for t in tasks if t["state"] == "needs_user"),
                "downloading_task_ids": sorted(t["id"] for t in tasks if t["state"] == "downloading"),
                "bytes_known_task_ids": sorted(self.bytes_known),
                "archive": dict(self.archive) if self.archive else None,
            },
        }
        if self.snapshots:
            previous = self.snapshots[-1]
            if previous["tasks"] == snapshot["tasks"] and previous["captcha_pending"] == snapshot["captcha_pending"]:
                return
        self.seq += 1
        snapshot["seq"] = self.seq
        self.snapshots.append(snapshot)

    @staticmethod
    def _public_task(task: dict[str, Any]) -> dict[str, Any]:
        # Deep-copy the mutable members so later mutations never rewrite history.
        clone = dict(task)
        clone["stage_detail"] = dict(task.get("stage_detail") or {})
        if task.get("user_challenge") is not None:
            clone["user_challenge"] = dict(task["user_challenge"])
        return clone

    # ------------------------------------------------------------------ drive

    def run(self) -> list[dict[str, Any]]:
        for record in self.records:
            message = record.get("message") or ""
            context = record.get("context") or {}
            timestamp = record.get("timestamp") or ""
            wall = _parse_timestamp(timestamp)
            if wall is not None and wall > self.clock:
                self.clock = wall

            handled = False
            handled |= self._handle_state(message, context, timestamp)
            handled |= self._handle_lifecycle(message, context, timestamp)
            handled |= self._handle_captcha(message, context, timestamp)
            handled |= self._handle_transfer(message, context, timestamp)
            self._handle_timer_tick(message, context)
            _ = handled
        return self.snapshots

    def _handle_state(self, message: str, context: dict[str, Any], timestamp: str) -> bool:
        match = STATE_RE.search(message)
        task_id = context.get("task_id")
        if not match or not task_id:
            return False
        task = self._task(task_id)
        state = match.group(1)
        task["state"] = state
        error = context.get("error")
        if error:
            task["error"] = error
        elif "error" in task:
            task.pop("error")
        if context.get("provider"):
            task["provider"] = context["provider"]
        # The engine clears the user-action gate when it leaves needs_user
        # (service.py _resolve_package_captcha sets user_challenge = {}).
        if state != "needs_user":
            task.pop("user_action", None)
            task.pop("user_challenge", None)
        # Reaching verifying/postprocessing/completed means the engine finished
        # the transfer, so completed_bytes == size is engine truth, not a guess.
        if state in {"verifying", "postprocessing", "completed"} and task.get("size"):
            task["completed_bytes"] = task["size"]
            self.bytes_known.add(task_id)
        self._emit(f"state:{state}", timestamp)
        return True

    def _handle_lifecycle(self, message: str, context: dict[str, Any], timestamp: str) -> bool:
        if "(lifecycle)" not in message:
            return False
        to_stage = context.get("to_stage")
        task_id = context.get("task_id")
        if not to_stage or not task_id:
            return False
        task = self._task(task_id)
        task["stage"] = to_stage
        task["stage_detail"] = dict(context.get("stage_detail") or {})
        entered = _as_float(context.get("entered_at_us"))
        if entered is not None:
            task["stage_entered_at"] = entered
            if entered > self.clock:
                self.clock = entered
        if context.get("provider"):
            task["provider"] = context["provider"]
        detail = task["stage_detail"]
        if detail.get("source") == "archive":
            self.archive = {
                "task_id": task_id,
                "event": detail.get("event"),
                "state": detail.get("state"),
                "operation": detail.get("operation"),
                "expected_size": detail.get("expected_size"),
                "progress_bytes": detail.get("progress_bytes"),
                "observed_size": detail.get("observed_size"),
                "stage": to_stage,
            }
        self._emit(f"lifecycle:{to_stage}", timestamp)
        return True

    def _handle_captcha(self, message: str, context: dict[str, Any], timestamp: str) -> bool:
        if "[CAPTCHA_CHALLENGE]" in message:
            challenge_id = context.get("challenge_id")
            task_id = context.get("task_id")
            if not challenge_id or not task_id:
                return False
            captcha_type = str(context.get("captcha_type") or "").split(".")[-1].lower()
            challenge = {
                "id": challenge_id,
                "task_id": task_id,
                "provider_id": context.get("provider") or "",
                "captcha_type": captcha_type,
                "params": {
                    key: context[key]
                    for key in ("site_key", "page_url")
                    if context.get(key) is not None
                },
                "timeout_seconds": 0,
                "created_at": round(self.clock, 6),
                "expires_at": 0,
                "time_remaining": 0,
                "status": "pending",
            }
            self.challenges[challenge_id] = challenge
            task = self._task(task_id)
            task["user_action"] = captcha_type or "captcha"
            task["user_challenge"] = {
                "id": challenge_id,
                "challenge_id": challenge_id,
                "captcha_type": captcha_type,
                "provider_id": challenge["provider_id"],
            }
            self._emit("captcha:challenge", timestamp)
            return True
        if "[CAPTCHA_SOLVED_SUCCESS]" in message or "[CAPTCHA_SOLVE_FAILED]" in message:
            challenge_id = context.get("challenge_id")
            if challenge_id and challenge_id in self.challenges:
                self.challenges.pop(challenge_id)
                # service.py clears user_challenge on every package member.
                for task in self.tasks.values():
                    challenge = task.get("user_challenge") or {}
                    if challenge.get("id") == challenge_id:
                        task.pop("user_challenge", None)
                        task.pop("user_action", None)
                self._emit("captcha:solved", timestamp)
                return True
        return False

    def _handle_transfer(self, message: str, context: dict[str, Any], timestamp: str) -> bool:
        task_id = context.get("task_id")
        if not task_id:
            return False
        if "[ITEM_START]" in message:
            task = self._task(task_id)
            size = context.get("bytes")
            if isinstance(size, int) and size > 0:
                task["size"] = size
            self._emit("transfer:item_start", timestamp)
            return True
        if "[ITEM_VERIFIED]" in message:
            task = self._task(task_id)
            task["integrity_state"] = "verified"
            if task.get("size"):
                task["completed_bytes"] = task["size"]
                self.bytes_known.add(task_id)
            self._emit("transfer:item_verified", timestamp)
            return True
        if "[DOWNLOAD_COMPLETED]" in message:
            task = self._task(task_id)
            if task.get("size"):
                task["completed_bytes"] = task["size"]
                self.bytes_known.add(task_id)
            self._emit("transfer:download_completed", timestamp)
            return True
        if "[DOWNLOAD_FAILED]" in message:
            self._emit("transfer:download_failed", timestamp)
            return True
        return False

    def _handle_timer_tick(self, message: str, context: dict[str, Any]) -> None:
        match = TIMER_TICK_RE.search(message)
        task_id = context.get("task_id")
        if not match or not task_id:
            return
        task = self.tasks.get(task_id)
        if not task or task.get("stage") != "hoster_wait_timer":
            return
        self.timer_observations.append(
            {
                "run": self.slug,
                "task_id": task_id,
                "at": round(self.clock, 6),
                "engine_remaining_seconds": int(match.group(1)),
                "stage": task.get("stage"),
                "stage_detail": dict(task.get("stage_detail") or {}),
                "stage_entered_at": task.get("stage_entered_at"),
            }
        )

    # ------------------------------------------------------------------ final

    def finalize(self) -> dict[str, Any]:
        """Engine ground truth for the whole run, asserted as a terminal check."""
        return {
            "run": self.slug,
            "source": str(self.source.relative_to(REPO_ROOT)).replace("\\", "/"),
            "final_states": self.summary.get("final_states")
            or {task_id: final.get("state") for task_id, final in (self.summary.get("finals") or {}).items()},
            "stage_sequences": self.summary.get("stages") or {},
            "archive_jobs": [
                {
                    "id": job.get("id"),
                    "task_id": job.get("task_id"),
                    "state": job.get("state"),
                    "error": job.get("error"),
                    "operation": job.get("operation"),
                    "expected_size": job.get("expected_size"),
                    "observed_size": job.get("observed_size"),
                    "progress_bytes": job.get("progress_bytes"),
                }
                for job in (self.summary.get("archive_jobs") or [])
            ],
            "distinct_challenge_count": self.summary.get("challenge_count"),
            "timer_observations": self.timer_observations,
        }


def slug_for(source: Path) -> str:
    parts = source.parts
    if "lifecycle-matrix" in parts:
        return f"lifecycle_{source.name}"
    return f"live_{source.name}"


def build(source: Path, out_dir: Path) -> tuple[Path, int]:
    slug = slug_for(source)
    replay = RunReplay(source, slug)
    snapshots = replay.run()
    header = {"kind": "meta", **replay.finalize(), "snapshot_count": len(snapshots)}
    out_path = out_dir / f"{slug}.ui_snapshots.jsonl"
    lines = [json.dumps(header, sort_keys=True)]
    lines.extend(json.dumps({"kind": "snapshot", **snapshot}, sort_keys=True) for snapshot in snapshots)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path, len(snapshots)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=None, help="Run artifact directory (repeatable).")
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="Fixture output directory.")
    parser.add_argument("--check", action="store_true", help="Fail if regeneration changes a committed fixture.")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = [REPO_ROOT / entry for entry in (args.source or DEFAULT_SOURCES)]

    stale: list[str] = []
    written = 0
    for source in sources:
        if not (source / "summary.json").is_file() or not (source / "telemetry.jsonl").is_file():
            print(f"  [SKIP] {source} (missing summary.json/telemetry.jsonl)")
            continue
        target = out_dir / f"{slug_for(source)}.ui_snapshots.jsonl"
        before = target.read_text(encoding="utf-8") if args.check and target.is_file() else None
        path, count = build(source, out_dir)
        written += 1
        if before is not None and before != path.read_text(encoding="utf-8"):
            stale.append(str(path))
        print(f"  [OK]   {path.name}: {count} snapshots from {source.name}")

    if not written:
        print("No run artifacts found.", file=sys.stderr)
        return 2
    if stale:
        print("Committed fixtures are stale:\n  " + "\n  ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
