"""Live end-to-end lifecycle harness.

Drives the *real* engine (real Clearcote solver, real DataNodes provider, real
transfer, real archive pipeline) from a example-repacks page through link
intake, resolve, wait timer, CAPTCHA, direct link, download, verify, unrar and
cleanup. Captures every telemetry record, times every lifecycle stage, and
writes a per-run artifact directory.

This is the live counterpart to ``tests/e2e/lifecycle_harness.py`` (which is
offline and deterministic). It intentionally performs network I/O.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.service import EngineService
from engine.telemetry import LogEvent, telemetry_bus
from tests.live.report import build_report, stage_durations, write_report

DEFAULT_HOME = "https://example-repacks.test/"
_PART_RE = re.compile(
    r"^https://datanodes\.to/[^/]+/(?P<base>.+?)[._-]part(?P<num>\d+)\.(?P<ext>rar|7z|zip)$",
    re.IGNORECASE,
)
_FF_PART_RE = re.compile(
    r"^https://fuckingfast\.co/[^/#]+#(?P<base>.+?)[._-]part(?P<num>\d+)\.(?P<ext>rar|7z|zip)$",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https://datanodes\.to/[^\"'<>\s]+")
_FF_URL_RE = re.compile(r"https://fuckingfast\.co/[^\"'<>\s]+")


@dataclass
class PackageInfo:
    name: str
    parts: dict[int, str] = field(default_factory=dict)
    hoster: str = "datanodes"

    @property
    def part_count(self) -> int:
        return len(self.parts)

    def ordered(self) -> list[tuple[int, str]]:
        return sorted(self.parts.items())

    def contiguous(self) -> bool:
        nums = sorted(self.parts)
        return nums == list(range(1, len(nums) + 1))


def preflight() -> dict[str, Any]:
    """Verify the live toolchain the harness depends on."""
    import os

    from engine.clearcote_manager import get_clearcote_executable

    localappdata = os.environ.get("LOCALAPPDATA", "")
    checks = {
        "clearcote": bool(get_clearcote_executable()),
        "seven_zip": Path(r"C:\Program Files\7-Zip\7z.exe").is_file(),
        "unrar": Path(r"C:\Program Files\WinRAR\UnRAR.exe").is_file(),
        "archive_worker": Path("src-tauri/target/release/archive-worker.exe").is_file()
        or Path("src-tauri/target/debug/archive-worker.exe").is_file(),
        "ffmpeg": Path(r"C:\ffmpeg\bin\ffmpeg.exe").is_file(),
        "localappdata": bool(localappdata),
    }
    return checks


class LinkIntake:
    """Fetch a examplepack page and discover DataNodes multipart packages."""

    def __init__(self, home: str = DEFAULT_HOME) -> None:
        self.home = home

    def fetch(self, url: str | None = None, timeout: float = 40.0) -> str:
        from engine import http_client

        resp = http_client.get(url or self.home, timeout=timeout)
        return resp.read().decode("utf-8", "replace")

    @staticmethod
    def discover(html: str) -> list[PackageInfo]:
        packages: dict[tuple[str, str], PackageInfo] = {}
        for url in _URL_RE.findall(html):
            match = _PART_RE.match(url)
            if not match:
                continue
            base = match.group("base")
            info = packages.setdefault((base, "datanodes"), PackageInfo(name=base, hoster="datanodes"))
            info.parts[int(match.group("num"))] = url
        for url in _FF_URL_RE.findall(html):
            match = _FF_PART_RE.match(url)
            if not match:
                continue
            base = match.group("base")
            info = packages.setdefault((base, "fuckingfast"), PackageInfo(name=base, hoster="fuckingfast"))
            info.parts[int(match.group("num"))] = url
        return list(packages.values())

    @staticmethod
    def select(packages: list[PackageInfo], *, name: str | None = None,
               min_parts: int = 3, hoster: str = "datanodes") -> PackageInfo:
        candidates = [p for p in packages
                      if p.hoster == hoster and p.part_count >= min_parts and p.contiguous()]
        if name:
            wanted = name.lower()
            candidates = [p for p in candidates if wanted in p.name.lower()]
        if not candidates:
            raise RuntimeError(f"no contiguous {hoster} package with >= {min_parts} parts"
                               + (f" matching '{name}'" if name else ""))
        # Default: fewest parts (smallest download) first.
        return min(candidates, key=lambda p: p.part_count)


class TelemetryTap:
    """Capture every telemetry record for the run."""

    def __init__(self) -> None:
        self.events: list[LogEvent] = []
        self._lock = threading.Lock()

    def __call__(self, event: LogEvent) -> None:
        with self._lock:
            self.events.append(event)

    def attach(self) -> None:
        telemetry_bus.add_listener(self)

    def detach(self) -> None:
        telemetry_bus.remove_listener(self)

    def timeline(self) -> list[dict[str, Any]]:
        return [
            {"timestamp": e.timestamp, "level": e.level, "subsystem": e.subsystem,
             "message": e.message, "context": e.context}
            for e in self.events
        ]


@dataclass
class StageDuration:
    task_id: str
    name: str
    stage: str
    entered_at: float
    held_seconds: float | None


class StageTimer:
    """Derive per-stage durations from the engine's persisted stage history.

    Thin adapter over :func:`tests.live.report.stage_durations`, which is the
    single source of truth for the ``held_seconds`` derivation (the report
    builder needs it for runs replayed from disk, where no Task object exists).
    """

    @staticmethod
    def durations(task: Any) -> list[StageDuration]:
        rows = stage_durations(task.id, task.display_name or task.source_url,
                               task.stage_history or [])
        return [StageDuration(**row) for row in rows]


def _new_run_dir(root: Path, name: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = root / f"{stamp}_{name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


class LiveRun:
    """One live multipart lifecycle run against the production engine."""

    def __init__(
        self,
        *,
        destination: Path,
        package_name: str | None = None,
        min_parts: int = 3,
        max_parts: int | None = None,
        home: str = DEFAULT_HOME,
        hoster: str = "datanodes",
        report_root: Path = Path("reports/live"),
        data_dir: Path | None = None,
        seed_real_profile: bool = True,
        interactive: bool = False,
        timeout_seconds: float = 3600.0,
        solve_timeout_seconds: float = 45.0,
    ) -> None:
        self.destination = Path(destination)
        self.package_name = package_name
        self.min_parts = min_parts
        self.max_parts = max_parts
        self.home = home
        self.hoster = hoster
        self.report_root = Path(report_root)
        self.data_dir = Path(data_dir) if data_dir else None
        self.seed_real_profile = seed_real_profile
        self.interactive = interactive
        self.timeout_seconds = timeout_seconds
        self.solve_timeout_seconds = solve_timeout_seconds
        self.run_dir: Path | None = None
        self.tap = TelemetryTap()
        self.service: EngineService | None = None
        self.package: PackageInfo | None = None
        self.task_ids: list[str] = []
        self.solves = 0
        self.started_at = 0.0

    # -- setup ---------------------------------------------------------------
    def _prepare(self) -> None:
        self.destination.mkdir(parents=True, exist_ok=True)
        self.run_dir = _new_run_dir(self.report_root, self.package_name or "live")
        data_dir = self.data_dir or (self.run_dir / "engine")
        data_dir.mkdir(parents=True, exist_ok=True)
        self.tap.attach()
        self.service = EngineService(data_dir)
        # Keep retries bounded for a live run; the harness reports failures.
        self.service.resources.policy.max_retries = 2
        if self.seed_real_profile:
            seeded = self._seed_real_profile()
            if self.run_dir is not None:
                (self.run_dir / "seeded_profile.json").write_text(
                    json.dumps(seeded, indent=2), encoding="utf-8")

    def _seed_real_profile(self) -> dict[str, Any]:
        """Load the running app's persisted host clearance + settings.

        The production app resolves hosters by replaying a persisted
        ``cf_clearance`` (so it usually needs no CAPTCHA). A fresh harness data
        dir has none, which forces the fragile challenge path. Seeding the real
        profile makes the harness exercise the *real* environment.
        """
        source = Path(os.environ.get("APPDATA", "")) / "ai.transfer.manager" / "downloads.sqlite3"
        seeded: dict[str, Any] = {"source": str(source), "exists": source.is_file(),
                                  "host_sessions": [], "settings": []}
        if not source.is_file() or self.service is None:
            return seeded
        try:
            con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT value_json FROM settings WHERE key='provider_host_sessions'").fetchone()
            if row:
                for host, data in json.loads(row["value_json"] or "{}").items():
                    if isinstance(data, dict):
                        self.service.set_host_session(host, data)
                        seeded["host_sessions"].append(host)
            for key in ("captcha_ui_preferences", "ui_settings", "active_route_profile"):
                row = con.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
                if row:
                    self.service.store.set_setting(key, json.loads(row["value_json"]))
                    seeded["settings"].append(key)
            con.close()
        except Exception as exc:
            seeded["error"] = f"{type(exc).__name__}: {exc}"
        return seeded

    def _intake(self) -> PackageInfo:
        assert self.run_dir is not None
        intake = LinkIntake(self.home)
        html = intake.fetch()
        (self.run_dir / "home.html").write_text(html, encoding="utf-8")
        packages = intake.discover(html)
        self.package = intake.select(packages, name=self.package_name, min_parts=self.min_parts,
                                     hoster=self.hoster)
        return self.package

    def _add_tasks(self) -> None:
        assert self.service is not None and self.package is not None
        parts = self.package.ordered()
        if self.max_parts:
            parts = parts[: self.max_parts]
        for _number, url in parts:
            created = self.service.dispatch("add_task", {
                "url": url,
                "destination": str(self.destination),
                "folder_path": str(self.destination),
                "browser_context": {"auto_extract": True},
            })
            self.task_ids.append(str(created["id"]))
        self.service.dispatch("resume_engine")

    # -- solve orchestration -------------------------------------------------
    def _drive_captcha(self, task: Any) -> None:
        assert self.service is not None
        challenge = task.user_challenge or {}
        cid = challenge.get("challenge_id") or challenge.get("id")
        if not cid:
            return
        self.solves += 1
        self.service.dispatch("captcha_solve", {"challenge_id": cid})

    def _run_loop(self) -> None:
        assert self.service is not None
        attempts: dict[str, int] = {}
        last_attempt: dict[str, float] = {}
        max_attempts = 4
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            tasks = [self.service.store.get(tid) for tid in self.task_ids]
            if all(t is not None and t.state in {"completed", "failed", "canceled"} for t in tasks):
                return
            for task in tasks:
                if not task or task.state != "needs_user":
                    continue
                challenge = task.user_challenge or {}
                cid = challenge.get("challenge_id") or challenge.get("id")
                if not cid:
                    continue
                done = attempts.get(cid, 0)
                if done >= max_attempts:
                    if self.interactive and time.time() - last_attempt.get(cid, 0) > 60:
                        last_attempt[cid] = time.time()
                        print(f"[manual] {task.id[:8]} unresolved after {done} attempts; "
                              f"complete the visible Clearcote window if shown")
                    continue
                if time.time() - last_attempt.get(cid, 0) < 15:
                    continue
                attempts[cid] = done + 1
                last_attempt[cid] = time.time()
                self._drive_captcha(task)
            time.sleep(1.0)

    def _await_archive(self, timeout: float = 900.0) -> None:
        """Wait for the multipart archive pipeline to finish extract + cleanup."""
        assert self.service is not None
        if not self.package or self.package.part_count <= 1:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            jobs = self.service.store.list_archive_jobs()
            if jobs and all(j.get("state") in {"completed", "failed", "canceled", "needs_user"} for j in jobs):
                return
            time.sleep(2)

    # -- reporting -----------------------------------------------------------
    def _collect(self) -> dict[str, Any]:
        assert self.service is not None and self.run_dir is not None
        tasks = [self.service.store.get(tid) for tid in self.task_ids]
        stages: dict[str, list[dict[str, Any]]] = {}
        finals: dict[str, Any] = {}
        durations: list[dict[str, Any]] = []
        for task in tasks:
            if not task:
                continue
            finals[task.id] = {
                "name": task.display_name, "state": task.state, "size": task.size,
                "completed_bytes": task.completed_bytes, "integrity_state": task.integrity_state,
                "providers": task.provider, "error": task.error,
            }
            stages[task.id] = [e.get("to") for e in (task.stage_history or [])]
            for d in StageTimer.durations(task):
                durations.append({
                    "task_id": d.task_id, "name": d.name, "stage": d.stage,
                    "entered_at": d.entered_at, "held_seconds": d.held_seconds,
                })
        archive_jobs = self.service.store.list_archive_jobs()
        dest_files = sorted(p.name for p in self.destination.iterdir()) if self.destination.exists() else []
        return {"finals": finals, "stages": stages, "durations": durations,
                "archive_jobs": archive_jobs, "destination_files": dest_files}

    def run(self) -> dict[str, Any]:
        result: dict[str, Any] = {"package": None, "tasks": self.task_ids}
        self.started_at = time.time()
        try:
            self._prepare()
            package = self._intake()
            result["package"] = {"name": package.name, "parts": package.part_count,
                                 "urls": {str(k): v for k, v in package.ordered()}}
            assert self.run_dir is not None
            (self.run_dir / "package.json").write_text(
                json.dumps(result["package"], indent=2), encoding="utf-8")
            self._add_tasks()
            self._run_loop()
            self._await_archive()
            result.update(self._collect())
            result["solves"] = self.solves
            if self.max_parts:
                result["max_parts"] = int(self.max_parts)
        finally:
            self.tap.detach()
            if self.service is not None:
                self.service.close()
        result["wall_seconds"] = round(time.time() - self.started_at, 3)
        timeline = self.tap.timeline()
        if self.run_dir is not None:
            (self.run_dir / "telemetry.jsonl").write_text(
                "\n".join(json.dumps(r, default=str, sort_keys=True) for r in timeline) + "\n",
                encoding="utf-8")
            result["run_dir"] = str(self.run_dir)
            # summary.json keeps the raw per-stage durations; report.json is the
            # benchmarked view built from them plus the telemetry timeline.
            (self.run_dir / "summary.json").write_text(
                json.dumps(result, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")
            report = build_report(
                result=result, timeline=timeline, started=self.started_at or None,
                wall_seconds=result["wall_seconds"], hoster=self.hoster,
                destination=str(self.destination), solves=self.solves, run_dir=self.run_dir)
            result["report"] = report
            result["report_path"] = str(write_report(report, self.run_dir))
        return result
