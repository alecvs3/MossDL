"""Execute one trial: submit, watch, (optionally kill+relaunch), verify, measure."""

from __future__ import annotations

import json
import shutil
import time
import urllib.request
from pathlib import Path

from . import payload
from .adapters.base import Adapter, Job, JobFile, pending_files, unexpected_files, wait_quiet
from .sampler import TreeSampler
from .server import hls_segment_seed


class ServerLink:
    """Talks to the harness server's /__stats and maps its clock onto ours."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.offset = 0.0  # server_time - local_time

    def _get(self, path: str) -> dict:
        before = time.time()
        with urllib.request.urlopen(self.base_url + path, timeout=5) as response:
            data = json.loads(response.read())
        after = time.time()
        if "now" in data:
            self.offset = data["now"] - (before + after) / 2
        return data

    def reset(self) -> None:
        self._get("/__reset")

    def stats(self) -> dict:
        return self._get("/__stats")

    def local(self, server_time: float | None) -> float | None:
        return None if server_time is None else server_time - self.offset


def build_job(scenario, base_url: str | None, dest: Path, policy: str, connections: int, concurrent: int) -> Job:
    if scenario.kind == "hls":
        count, size, seed = scenario.hls_segments, scenario.hls_segment_size, 7
        url = f"{base_url}/hls/{count}/{size}/{seed}/index.m3u8"
        files = [JobFile("hls-vod.ts", url, count * size, _hls_sha256(count, size, seed))]
    elif scenario.kind == "public":
        spec = scenario.files[0]
        files = [JobFile(spec.name, scenario.public_url, spec.size, scenario.public_sha256)]
    else:
        files = [JobFile(spec.name, f"{base_url}/f/{spec.size}/{spec.seed}/{spec.name}", spec.size,
                         payload.sha256(spec.size, spec.seed)) for spec in scenario.files]
    equalized = policy == "equalized"
    return Job("hls" if scenario.kind == "hls" else "http", files, dest, policy,
               connections if equalized else None, concurrent if equalized else None)


def _hls_sha256(count: int, size: int, seed: int) -> str:
    import hashlib
    digest = hashlib.sha256()
    for index in range(count):
        for piece in payload.read_range(size, hls_segment_seed(seed, index), 0, size - 1):
            digest.update(piece)
    return digest.hexdigest()


def verify(job: Job) -> dict:
    problems, ok = [], 0
    for spec in job.files:
        target = job.dest / spec.name
        if not target.is_file():
            problems.append(f"{spec.name}: missing")
            continue
        if spec.size and target.stat().st_size != spec.size:
            problems.append(f"{spec.name}: size {target.stat().st_size} != {spec.size}")
            continue
        if spec.sha256 and payload.file_sha256(target) != spec.sha256.lower():
            problems.append(f"{spec.name}: sha256 mismatch")
            continue
        if not spec.sha256:
            problems.append(f"{spec.name}: no reference sha256 (size-only check)")
        ok += 1
    extras = unexpected_files(job)
    if extras:
        problems.append(f"unexpected files: {extras[:5]}{' ...' if len(extras) > 5 else ''}")
    return {"files_total": len(job.files), "files_ok": ok, "all_ok": ok == len(job.files), "problems": problems}


def run(adapter: Adapter, scenario, job: Job, record: dict, link: ServerLink | None,
        sample_interval: float = 0.25, poll_interval: float = 0.1) -> dict:
    shutil.rmtree(job.dest, ignore_errors=True)
    job.dest.mkdir(parents=True, exist_ok=True)
    if link:
        link.reset()
    names = getattr(adapter, "idle_process_names", adapter.process_names) if scenario.kind == "idle" else adapter.process_names
    timing = record["timing"]
    if scenario.kind != "idle":
        try:
            adapter.prepare(job)
        except Exception as exc:
            record.update(status="error", error=f"prepare failed: {type(exc).__name__}: {exc}"[:800])
            return record
    sampler = TreeSampler(pids=adapter.pids(), names=names, interval=sample_interval).start()
    if scenario.kind == "idle":
        time.sleep(scenario.idle_seconds)
        record["resources"] = sampler.stop().to_dict()
        record["correctness"].update(files_total=0, files_ok=0, all_ok=True)
        if record["resources"]["max_processes"]:
            record["status"] = "ok"
        else:
            record.update(status="error", error=f"none of {names} running: launch the app before this scenario")
        return record
    timing["submitted_at"] = time.time()
    try:
        adapter.start(job)
    except Exception as exc:
        record["resources"] = sampler.stop().to_dict()
        record.update(status="error", error=f"start failed: {type(exc).__name__}: {exc}"[:800])
        return record
    for pid in adapter.pids():
        sampler.add_pid(pid)
    deadline = time.time() + scenario.timeout_s
    remaining = {spec.name for spec in job.files}
    total = max(1, scenario.total_bytes)
    stats: dict = {}
    killed = False
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            client = adapter.poll()
        except Exception as exc:  # client API hiccup: note it, keep using file evidence
            client = None
            record["notes"].append(f"poll error: {type(exc).__name__}: {exc}"[:200])
        if client and client.get("failed"):
            record.update(status="failed", error=str(client["failed"])[:800])
            break
        if link:
            stats = link.stats()
        if scenario.kind == "resume" and not killed and stats.get("bytes_sent", 0) >= scenario.resume_at_fraction * total:
            adapter.kill()
            timing["killed_at"] = time.time()
            killed = True
            time.sleep(2.0)
            adapter.relaunch(job)
            timing["relaunched_at"] = time.time()
            for pid in adapter.pids():
                sampler.add_pid(pid)
            continue
        remaining = pending_files(job, remaining)
        server_idle = not link or stats.get("active", 0) == 0
        client_done = client is None or client.get("done", True)
        if not remaining and server_idle and client_done:
            candidate = time.time()
            if wait_quiet([job.dest / spec.name for spec in job.files[:50]], quiet_s=0.5, timeout_s=10):
                timing["completed_at"] = candidate
                record["status"] = "ok"
                break
    else:
        record.update(status="timeout", error=f"{len(remaining)} file(s) incomplete after {scenario.timeout_s:.0f}s")
    record["resources"] = sampler.stop().to_dict()
    if link:
        stats = link.stats()
        timing["first_byte_at"] = link.local(stats.get("first_payload_at"))
        timing["last_byte_at"] = link.local(stats.get("last_payload_at"))
        sent = stats.get("bytes_sent", 0)
        record["server"] = {key: stats.get(key) for key in ("requests", "range_requests", "peak_active", "bytes_sent",
                                                            "rejected_overlimit", "aborted", "user_agents")}
        record["server"]["overhead_ratio"] = (sent / scenario.total_bytes - 1) if scenario.total_bytes else None
    if scenario.kind == "resume" and not killed:
        record["notes"].append("kill point never reached (download finished before the threshold)")
    record["correctness"] = verify(job)
    if record["status"] == "ok" and not record["correctness"]["all_ok"]:
        record.update(status="failed", error="integrity check failed")
    return record
