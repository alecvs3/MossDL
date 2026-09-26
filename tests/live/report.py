"""Benchmarked end-to-end report builder for live lifecycle runs.

Turns the artifacts a :class:`tests.live.live_harness.LiveRun` produces (stage
history, telemetry timeline, archive jobs, per-download benchmark logs) into the
``report.json`` described in section 6 of the multipart master plan.

Every marker parsed here was verified against its emitting source: CAPTCHA_*
(engine/captcha.py), CHALLENGE_*/SOLVER_*/TIMER_DETECTED/TIMER_PARSE_MISS/
TIMER_DETECT_FAILED (engine/browser_solver.py, engine/providers/cyberdrop_hosts.py),
TIMER_ARMED/TIMER_SKIP/TIMER_TICK (engine/timer_scheduler.py), CONCURRENCY_AUDIT
(engine/concurrency_auditor.py) and DOWNLOAD_COMPLETED/DISPATCH/ITEM_VERIFIED
(engine/transfer). Nothing is silently dropped: every record the parser cannot
attribute is appended to ``report["notes"]`` with the disqualifying reason.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from tests.live.resolve_phase import build_resolve_phase
from tests.live.utilization import build_utilization

_TASK_PREFIX = re.compile(r"^\[[0-9a-f]{6,12}\]\s*")
_MARKER = re.compile(r"^\[([A-Z][A-Z0-9_]{2,})\]")
_TRACEBACK = re.compile(r"Traceback \(most recent call last\)")

# marker -> (track, outcome). Two independent solver tracks are emitted live:
# the provider plugin pairs [CAPTCHA_DETECTED] with [CAPTCHA_BYPASSED] /
# [CHALLENGE_TIMEOUT] keyed by task_id, while engine/captcha.py pairs
# [CAPTCHA_CHALLENGE] with [CAPTCHA_SOLVED_SUCCESS] keyed by challenge_id
# (its success record carries no task_id, so task keying cannot match it).
SOLVER_MARKERS = {
    "CAPTCHA_DETECTED": ("provider", "open"),
    "CAPTCHA_BYPASSED": ("provider", "solved"),
    "CHALLENGE_TIMEOUT": ("provider", "timeout"),
    "CAPTCHA_CHALLENGE": ("engine", "open"),
    "CAPTCHA_SOLVED_SUCCESS": ("engine", "solved"),
}
TIMER_DETECT = ("TIMER_ARMED", "TIMER_DETECTED")
TIMER_MISS = ("TIMER_PARSE_MISS", "TIMER_DETECT_FAILED", "TIMER_SKIP")
NAME_MARKERS = ("DOWNLOAD_COMPLETED", "DISPATCH", "ITEM_VERIFIED", "SESSION_INIT")


def parse_ts(value: Any) -> float | None:
    """Epoch seconds from a telemetry ISO-8601 ``timestamp`` (or a float)."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def marker_of(message: str) -> str | None:
    """``"[64d1513c] [TIMER_TICK] ..."`` -> ``"TIMER_TICK"``."""
    found = _MARKER.match(_TASK_PREFIX.sub("", message or ""))
    return found.group(1) if found else None


def stage_durations(task_id: str, name: str, history: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-stage ``held_seconds`` derived from a task's persisted stage history."""
    entries, out = list(history or []), []
    for index, entry in enumerate(entries):
        entered = float(entry.get("entered_at") or 0.0)
        nxt = entries[index + 1].get("entered_at") if index + 1 < len(entries) else None
        held = (float(nxt) - entered) if (nxt and entered) else None
        out.append({"task_id": task_id, "name": name, "stage": str(entry.get("to") or ""),
                    "entered_at": entered,
                    "held_seconds": round(held, 3) if held is not None else None})
    return out


def _union_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return round(sum(end - start for start, end in merged), 3)


def _percentile(values: Sequence[float], q: float) -> float | None:
    ordered = sorted(v for v in values if isinstance(v, (int, float)))
    if not ordered:
        return None
    pos = (len(ordered) - 1) * q
    low, high = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (pos - low), 2)


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if epoch else None


class LiveReportBuilder:
    """Assemble ``report.json`` from one live run's artifacts."""

    def __init__(self, *, result: dict[str, Any], timeline: Sequence[dict[str, Any]],
                 started: float | str | None = None, wall_seconds: float | None = None,
                 hoster: str = "", destination: str = "", solves: int | None = None,
                 run_dir: Path | None = None, copy_download_logs: bool = True) -> None:
        self.result = result or {}
        self.timeline = [dict(e, _epoch=parse_ts(e.get("timestamp")),
                              _marker=marker_of(e.get("message") or "")) for e in (timeline or [])]
        self.durations = list(self.result.get("durations") or [])
        self.finals = dict(self.result.get("finals") or {})
        self.started, self.wall_seconds = started, wall_seconds
        self.hoster, self.destination = hoster, destination
        self.solves = self.result.get("solves") if solves is None else solves
        self.run_dir = Path(run_dir) if run_dir else None
        self.copy_download_logs = copy_download_logs
        self.notes: list[dict[str, Any]] = []

    # -- helpers -------------------------------------------------------------
    def _note(self, code: str, **detail: Any) -> None:
        self.notes.append({"code": code, **detail})

    def _events(self, *markers: str) -> list[dict[str, Any]]:
        return [e for e in self.timeline if e.get("_marker") in set(markers)]

    @staticmethod
    def _ctx_task(event: dict[str, Any]) -> str | None:
        raw = (event.get("context") or {}).get("task_id")
        return str(raw).split(":")[0] if raw else None

    def _names(self) -> dict[str, str]:
        names = {tid: (info.get("name") or "") for tid, info in self.finals.items()}
        for event in self._events(*NAME_MARKERS):
            tid = self._ctx_task(event)
            display = (event.get("context") or {}).get("display_name")
            if tid and display and not names.get(tid):
                names[tid] = str(display)
        for row in self.durations:
            if row.get("name") and not names.get(row.get("task_id")):
                names[str(row["task_id"])] = str(row["name"])
        return names

    # -- sections ------------------------------------------------------------
    def _stages(self) -> dict[str, dict[str, Any]]:
        by_stage: dict[str, dict[str, Any]] = {}
        open_ended = 0
        for row in self.durations:
            stage, entered = str(row.get("stage") or ""), float(row.get("entered_at") or 0.0)
            if not stage or not entered:
                self._note("stage_row_unusable", stage=stage, task_id=row.get("task_id"),
                           reason="stage row has no stage name or no entered_at")
                continue
            held = row.get("held_seconds")
            if held is None:
                open_ended += 1
                held = 0.0
            slot = by_stage.setdefault(stage, {"intervals": [], "task_ids": set()})
            slot["intervals"].append((entered, entered + float(held)))
            slot["task_ids"].add(str(row.get("task_id") or ""))
        if open_ended:
            self._note("stage_intervals_open_ended", count=open_ended,
                       reason="terminal stage has no successor entered_at; counted as zero-length")
        return {stage: {"seconds": _union_seconds(slot["intervals"]),
                        "started": _iso(min(i[0] for i in slot["intervals"])),
                        "ended": _iso(max(i[1] for i in slot["intervals"])),
                        "task_ids": sorted(slot["task_ids"])}
                for stage, slot in sorted(by_stage.items())}

    def _download_logs(self) -> dict[str, dict[str, Any]]:
        logs: dict[str, dict[str, Any]] = {}
        for event in self._events("DOWNLOAD_COMPLETED"):
            tid, ctx = self._ctx_task(event), event.get("context") or {}
            if not tid:
                self._note("download_completed_without_task", message=event.get("message"),
                           reason="[DOWNLOAD_COMPLETED] context carries no task_id")
                continue
            logs[tid] = {"task_id": tid, "source_json": ctx.get("json_log"),
                         "source_log": ctx.get("readable_log"), "avg_mb_s": ctx.get("avg_speed_mb_s"),
                         "peak_mb_s": ctx.get("peak_speed_mb_s"), "stalls": ctx.get("stalls_count")}
        return logs

    def _benchmark(self, entry: dict[str, Any]) -> dict[str, Any]:
        source, tid = entry.get("source_json"), entry.get("task_id")
        path = Path(source) if source else None
        if not path or not path.is_file():
            self._note("benchmark_log_missing", task_id=tid, path=source,
                       reason="per-download benchmark JSON not readable; p95 unavailable")
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._note("benchmark_log_unreadable", task_id=tid, path=source,
                       reason=f"{type(exc).__name__}: {exc}")
            return {}
        summary = payload.get("summary") or {}
        p95 = summary.get("p95_speed_mb_s")
        if p95 is None:
            p95 = _percentile([s.get("speed_mb_per_sec")
                               for s in payload.get("second_by_second_telemetry") or []], 0.95)
            self._note("benchmark_p95_derived", task_id=tid,
                       reason="summary.p95_speed_mb_s absent; recomputed from per-second samples")
        return {"p95": p95, "seconds": summary.get("total_duration_seconds"),
                "stability": summary.get("stability_rating")}

    def _per_task(self, stage_seconds: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
        names, logs = self._names(), self._download_logs()
        out: list[dict[str, Any]] = []
        for tid in sorted(set(self.finals) | set(logs)):
            info, entry = self.finals.get(tid) or {}, logs.get(tid) or {}
            if not entry:
                self._note("task_without_download_log", task_id=tid,
                           reason="no [DOWNLOAD_COMPLETED] telemetry for this task; throughput unknown")
            bench = self._benchmark(entry) if entry else {}
            out.append({"task_id": tid, "name": names.get(tid) or None,
                        "size": info.get("size"), "bytes": info.get("completed_bytes"),
                        "state": info.get("state"), "integrity_state": info.get("integrity_state"),
                        "mb_s_avg": entry.get("avg_mb_s"), "mb_s_peak": entry.get("peak_mb_s"),
                        "mb_s_p95": bench.get("p95"), "stalls": entry.get("stalls"),
                        "seconds": bench.get("seconds"), "stability": bench.get("stability"),
                        "stage_seconds": stage_seconds.get(tid, {})})
        return sorted(out, key=lambda t: (t.get("name") or "", t["task_id"]))

    def _solvers(self) -> list[dict[str, Any]]:
        opened: dict[tuple[str, str], dict[str, Any]] = {}
        done: list[dict[str, Any]] = []
        for event in self.timeline:
            marker, ctx = event.get("_marker"), event.get("context") or {}
            if marker not in SOLVER_MARKERS:
                continue
            track, outcome = SOLVER_MARKERS[marker]
            tid, cid, at = self._ctx_task(event), ctx.get("challenge_id"), event.get("_epoch")
            key = (track, str(cid or tid or "")) if track == "engine" else (track, str(tid or ""))
            if not key[1]:
                self._note("solver_event_unattributable", marker=marker, track=track,
                           reason="marker carries neither task_id nor challenge_id")
                continue
            if outcome == "open":
                opened.setdefault(key, {"track": track, "task_id": tid, "challenge_id": cid,
                                        "challenge_at": _iso(at), "_at": at, "solved_at": None,
                                        "duration_ms": None, "duration_exact": False,
                                        "solver": ctx.get("engine"), "outcome": "unresolved"})
                continue
            record = opened.pop(key, None)
            if record is None:
                self._note("solver_outcome_without_open_challenge", marker=marker, track=track,
                           task_id=tid, challenge_id=cid,
                           reason="terminal solver marker seen with no matching open challenge")
                record = {"track": track, "task_id": tid, "challenge_id": cid, "challenge_at": None,
                          "_at": None, "solved_at": None, "duration_ms": None,
                          "duration_exact": False, "solver": None, "outcome": "unresolved"}
            record.update(outcome=outcome, solved_at=_iso(at),
                          task_id=record.get("task_id") or tid,
                          challenge_id=record.get("challenge_id") or cid,
                          solver=ctx.get("solver") or ctx.get("engine") or record.get("solver"))
            if ctx.get("duration_ms") is not None:
                record.update(duration_ms=round(float(ctx["duration_ms"]), 1), duration_exact=True)
            elif record["_at"] and at:
                # Telemetry timestamps are second-resolution, so this is a bound.
                record.update(duration_ms=round((at - record["_at"]) * 1000.0, 1), duration_exact=False)
            done.append(record)
        for key, record in opened.items():
            self._note("solver_challenge_never_resolved", track=key[0], key=key[1],
                       reason="challenge opened but no solved/timeout marker followed")
            done.append(record)
        for record in done:
            record.pop("_at", None)
        return sorted(done, key=lambda r: (r.get("challenge_at") or "~", r.get("task_id") or ""))

    def _timers(self) -> list[dict[str, Any]]:
        ticks: dict[str, list[float]] = {}
        for event in self._events("TIMER_TICK"):
            tid, at = self._ctx_task(event), event.get("_epoch")
            if tid and at:
                ticks.setdefault(tid, []).append(at)
        episodes: list[tuple[str, dict[str, Any]]] = []
        for event in self._events(*TIMER_DETECT, *TIMER_MISS):
            tid = self._ctx_task(event)
            if not tid:
                self._note("timer_event_without_task", marker=event.get("_marker"),
                           reason="timer marker carries no task_id; cannot attribute")
                continue
            episodes.append((tid, event))
        out: list[dict[str, Any]] = []
        for index, (tid, event) in enumerate(episodes):
            marker, ctx = event.get("_marker"), event.get("context") or {}
            at = event.get("_epoch")
            # A wait belongs to one episode only: bound it by the next timer
            # event for the same task so a later countdown's ticks cannot leak in.
            nxt = next((e.get("_epoch") for t, e in episodes[index + 1:] if t == tid), None)
            window = [t for t in ticks.get(tid, [])
                      if at and t >= at and (nxt is None or t < nxt)]
            detected = marker in TIMER_DETECT
            waited = round(max(window) - at, 3) if (window and at) else (None if detected else 0.0)
            if waited is None:
                self._note("timer_wait_unobserved", task_id=tid, marker=marker,
                           reason="timer armed but no [TIMER_TICK] followed; waited_seconds unknown")
            out.append({"task_id": tid, "detected_at": _iso(at), "marker": marker,
                        "countdown_seconds": ctx.get("seconds") if detected else 0,
                        "waited_seconds": waited,
                        "source": ctx.get("source") or (f"{marker.lower()}:{ctx['reason']}"
                                                        if ctx.get("reason") else marker.lower())})
        return out

    def _concurrency_ramp(self) -> list[dict[str, Any]]:
        ceilings: dict[str, int | None] = {}
        earliest: dict[str, int] = {}
        out: list[dict[str, Any]] = []
        for event in self._events("CONCURRENCY_AUDIT"):
            ctx = event.get("context") or {}
            host = str(ctx.get("host") or "")
            for key in ("new_verified_ceiling", "verified_ceiling", "current_verified"):
                if ctx.get(key) is not None:
                    ceilings[host] = int(ctx[key])
                    break
            if host not in earliest and ctx.get("previous_ceiling") is not None:
                earliest[host] = int(ctx["previous_ceiling"])
            active = ctx.get("active_count")
            out.append({"at": _iso(event.get("_epoch")), "event": ctx.get("event"), "host": host,
                        "active_streams": len(ctx.get("active_tasks") or []) if active is None else active,
                        "ceiling": ceilings.get(host)})
        for row in out:  # backfill rows seen before the host's first ceiling report
            if row["ceiling"] is None and row["host"] in earliest:
                row["ceiling"] = earliest[row["host"]]
        return out

    def _archive_jobs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for job in self.result.get("archive_jobs") or []:
            created, updated = job.get("created_at"), job.get("updated_at")
            seconds = round(float(updated) - float(created), 3) if (created and updated) else None
            if seconds is None:
                self._note("archive_job_untimed", job_id=job.get("id"),
                           reason="archive job row lacks created_at/updated_at")
            out.append({"id": job.get("id"), "task_id": job.get("task_id"), "state": job.get("state"),
                        "seconds": seconds,
                        "stage_detail": {"operation": job.get("operation"), "format": job.get("format"),
                                         "attempt": job.get("attempt"), "error": job.get("error"),
                                         "recovery_reason": job.get("recovery_reason")}})
        return out

    def _downloads(self) -> list[dict[str, Any]]:
        target = (self.run_dir / "download_logs") if (self.run_dir and self.copy_download_logs) else None
        if target is not None:
            target.mkdir(parents=True, exist_ok=True)
        out: list[dict[str, Any]] = []
        for tid, entry in sorted(self._download_logs().items()):
            row: dict[str, Any] = {"task_id": tid, "source_json": entry.get("source_json"),
                                   "source_log": entry.get("source_log"), "json": None, "log": None}
            for kind in ("json", "log"):
                source = entry.get(f"source_{kind}")
                if not source:
                    self._note("download_log_ref_missing", task_id=tid, kind=kind,
                               reason="[DOWNLOAD_COMPLETED] context had no path for this log kind")
                    continue
                src = Path(source)
                if target is None:
                    row[kind] = str(src)
                elif not src.is_file():
                    self._note("download_log_not_copied", task_id=tid, path=source,
                               reason="source benchmark log no longer on disk")
                else:
                    shutil.copy2(src, target / src.name)
                    row[kind] = str(Path("download_logs") / src.name)
            out.append(row)
        return out

    def _assertions(self, per_task: list[dict[str, Any]], archive_jobs: list[dict[str, Any]],
                    solvers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        files = list(self.result.get("destination_files") or [])
        bad_state = [t["task_id"] for t in per_task if t.get("state") != "completed"]
        # `size_verified` passes but is NOT content proof; the report says which
        # kind of assurance the run actually has rather than flattening both to
        # a green tick.
        unverified = [t["task_id"] for t in per_task
                      if t.get("integrity_state") not in {"verified", "size_verified"}]
        size_only = [t["task_id"] for t in per_task
                     if t.get("integrity_state") == "size_verified"]
        bad_archive = [j["id"] for j in archive_jobs if j.get("state") != "completed"]
        short = [t["task_id"] for t in per_task if t.get("size") != t.get("bytes")]
        timeouts = [s["task_id"] for s in solvers if s.get("outcome") == "timeout"]
        solves = int(self.solves or 0)
        errors = [f"{e.get('subsystem')}: {e.get('message')}" for e in self.timeline
                  if str(e.get("level", "")).upper() == "ERROR"
                  or _TRACEBACK.search(str(e.get("message") or ""))]
        # A --max-parts smoke fetches a prefix of the package, so extraction is
        # expected to fail on the missing volumes; the archive assertion only
        # applies to full-package runs.
        partial = bool(self.result.get("max_parts"))
        checks = [
            ("all_tasks_completed", not bad_state and bool(per_task),
             f"{len(per_task) - len(bad_state)}/{len(per_task)} completed"
             + (f"; not completed: {bad_state}" if bad_state else "")),
            ("integrity_verified", not unverified and bool(per_task),
             f"not verified: {unverified}" if unverified
             else (f"length-checked only, no provider checksum ({len(size_only)}/{len(per_task)} "
                   f"part(s)); content proven by extraction, not by us"
                   if size_only else "all checksum-verified")),
            ("archive_jobs_completed", partial or not bad_archive,
             (f"skipped: partial run (first {self.result.get('max_parts')} part(s))"
              if partial else
              f"{len(archive_jobs)} job(s)" + (f"; not completed: {bad_archive}" if bad_archive else ""))),
            ("destination_files_present", len(files) > 0, f"{len(files)} file(s) in destination"),
            ("bytes_match_size", not short and bool(per_task),
             "completed_bytes == size for every task" if not short else f"short: {short}"),
            ("solves_iff_challenge", (solves > 0) == bool(solvers),
             f"solves={solves}, challenges observed={len(solvers)}"),
            ("no_solver_timeout", not timeouts,
             "no solver timeout" if not timeouts else f"timed out: {timeouts}"),
            ("telemetry_clean", not errors,
             "no ERROR/Traceback records" if not errors else f"{len(errors)} error(s): {errors[:3]}"),
        ]
        return [{"id": cid, "passed": bool(ok), "detail": detail} for cid, ok, detail in checks]

    # -- entry point ---------------------------------------------------------
    def build(self) -> dict[str, Any]:
        package = self.result.get("package") or {}
        per_stage_per_task: dict[str, dict[str, float]] = {}
        for row in self.durations:
            tid, stage = str(row.get("task_id") or ""), str(row.get("stage") or "")
            if tid and stage:
                bucket = per_stage_per_task.setdefault(tid, {})
                bucket[stage] = round(bucket.get(stage, 0.0) + float(row.get("held_seconds") or 0.0), 3)
        stages = self._stages()
        per_task = self._per_task(per_stage_per_task)
        solvers, archive_jobs = self._solvers(), self._archive_jobs()
        timers, ramp = self._timers(), self._concurrency_ramp()
        assertions = self._assertions(per_task, archive_jobs, solvers)
        epochs = [e["_epoch"] for e in self.timeline if e.get("_epoch")]
        started = self.started if isinstance(self.started, str) else _iso(
            self.started or (min(epochs) if epochs else None))
        wall = self.wall_seconds
        if wall is None and len(epochs) >= 2:
            wall = round(max(epochs) - min(epochs), 3)
        return {
            "schema": 1,
            "run": {"started": started, "wall_seconds": wall, "package": package.get("name"),
                    "parts": len(per_task) or package.get("parts"), "hoster": self.hoster or None,
                    "destination": str(self.destination) or None},
            "stages": stages, "per_task": per_task, "solvers": solvers, "timers": timers,
            "concurrency_ramp": ramp, "archive_jobs": archive_jobs,
            "downloads": self._downloads(), "assertions": assertions, "notes": self.notes,
            "resolve_phase": build_resolve_phase(self.timeline),
            "utilization": build_utilization(
                solvers=[{**row,
                          "_challenge_epoch": parse_ts(row.get("challenge_at")),
                          "_solved_epoch": parse_ts(row.get("solved_at"))} for row in solvers],
                timers=[{**row, "_detected_epoch": parse_ts(row.get("detected_at"))} for row in timers],
                ramp=[{**row, "_epoch": parse_ts(row.get("at"))} for row in ramp],
                started_epoch=min(epochs) if epochs else None,
                ended_epoch=max(epochs) if epochs else None,
                ready_tasks=len(per_task),
            ),
            "verdict": "pass" if all(a["passed"] for a in assertions) else "fail",
        }


def build_report(**kwargs: Any) -> dict[str, Any]:
    """Build the report dict; see :class:`LiveReportBuilder` for arguments."""
    return LiveReportBuilder(**kwargs).build()


def write_report(report: dict[str, Any], run_dir: Path) -> Path:
    path = Path(run_dir) / "report.json"
    path.write_text(json.dumps(report, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_run_artifacts(run_dir: Path) -> dict[str, Any]:
    """Rebuild builder inputs from an already-captured run directory.

    Returns ``{"result": ..., "timeline": ...}`` ready for :func:`build_report`.
    ``durations`` are recovered from the run's own engine database because runs
    captured before this builder existed dropped them from ``summary.json``.
    """
    run_dir = Path(run_dir)
    result = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    timeline = [json.loads(line) for line in
                (run_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if not result.get("durations"):
        database, durations = run_dir / "engine" / "downloads.sqlite3", []
        if not database.is_file():
            raise FileNotFoundError(f"no durations in summary.json and no engine DB at {database}")
        con = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        for row in con.execute("SELECT id, display_name, source_url, stage_history_json FROM tasks"):
            durations.extend(stage_durations(row["id"], row["display_name"] or row["source_url"] or "",
                                             json.loads(row["stage_history_json"] or "[]")))
        con.close()
        result["durations"] = durations
    return {"result": result, "timeline": timeline}
