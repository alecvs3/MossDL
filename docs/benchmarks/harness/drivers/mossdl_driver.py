"""Out-of-process MossDL driver, so the sampler measures MossDL's own process tree.

Modes
  core    RustTransferBackend (the shipped transfer-core.exe over its JSON-RPC
          session) with the engine's production defaults: connectionsPerFile=8,
          min_segment_size=16 MiB, maxConcurrent=6. HLS uses the engine's
          MediaAssembler. Excludes provider resolution, queueing and the UI.
  engine  A private EngineService (isolated data dir): add_task + download_task
          exactly as the UI does, polling the task store until terminal.

Protocol: one JSON object per stdout line prefixed with ``@@BENCH ``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(os.environ.get("MOSSDL_REPO") or Path(__file__).resolve().parents[4])
sys.path.insert(0, str(REPO))

TERMINAL = {"completed", "failed", "canceled"}
DEFAULT_CONNECTIONS, DEFAULT_CONCURRENT, MIN_SEGMENT = 8, 6, 16 * 1024 * 1024


def emit(event: str, **fields) -> None:
    sys.stdout.write("@@BENCH " + json.dumps({"event": event, "t": time.time(), **fields}) + "\n")
    sys.stdout.flush()


def wait_for_go() -> None:
    """Report readiness, then block until the harness says go (a warm, idle app)."""
    emit("ready")
    if sys.stdin.readline().strip() != "go":
        raise SystemExit("harness closed stdin before 'go'")


def run_core(job: dict, resume: bool) -> bool:
    from engine import rust_session
    from engine.media_pipeline import MediaAssembler, parse_media
    from engine.models import ResolvedItem
    from engine.rust_backend import RustTransferBackend

    # The shipped app keeps one transfer-core process alive; start it now so the
    # measured window does not include process creation.
    try:
        rust_session.session().request("tunnel_status", {}, 10)
    except Exception as exc:  # warm-up is best effort; its failure is reported
        emit("note", message=f"core warm-up request failed: {type(exc).__name__}: {exc}"[:300])
    if not resume:  # a relaunch is timed from process start, like a real restart
        wait_for_go()
    connections = job.get("connections") or DEFAULT_CONNECTIONS
    dest = Path(job["dest"])

    def one(spec: dict) -> str:
        if job["kind"] == "hls":
            with urllib.request.urlopen(spec["url"], timeout=30) as response:
                plan = parse_media(spec["url"], response.read().decode("utf-8", "replace"))
            if plan.selected_variant and not plan.segments:
                variant = plan.selected_variant["url"]
                with urllib.request.urlopen(variant, timeout=30) as response:
                    plan = parse_media(variant, response.read().decode("utf-8", "replace"))
            result = MediaAssembler().assemble(plan, dest / spec["name"])
            return f"segments={result.get('segments')}"
        backend = RustTransferBackend(max_segments=connections, min_segment_size=MIN_SEGMENT, max_retries=3)
        if not backend.available():
            raise RuntimeError("transfer-core binary not found (set TRANSFER_CORE_PATH)")
        item = ResolvedItem("generic", spec["url"], spec["name"], size=spec.get("size") or None,
                            direct_url=spec["url"])
        backend.download(item, dest)
        return f"segments={backend.last_segments}"

    workers = job.get("max_concurrent") or DEFAULT_CONCURRENT
    ok = True
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, spec): spec["name"] for spec in job["files"]}
        for future in as_completed(futures):
            try:
                emit("file_done", name=futures[future], detail=future.result())
            except Exception as exc:  # recorded, never swallowed
                ok = False
                emit("file_error", name=futures[future], message=f"{type(exc).__name__}: {exc}"[:500])
    return ok


def run_engine(job: dict, data_dir: Path, resume: bool) -> bool:
    from engine.service import EngineService

    service = EngineService(data_dir)
    try:
        changes = {}
        if job.get("connections"):
            changes["network"] = {"connectionsPerFile": job["connections"]}
        if job.get("max_concurrent"):
            changes["general"] = {"maxConcurrent": job["max_concurrent"]}
        if changes:
            service.dispatch("ui_settings_update", {"settings": changes})
        if not resume:
            wait_for_go()
        ids: dict[str, str] = {}
        if resume:
            for task in service.store.list():
                ids[task.id] = task.display_name or task.id
                if task.state not in TERMINAL:
                    service.dispatch("resume_task", {"id": task.id})
        else:
            for spec in job["files"]:
                task = service.dispatch("add_task", {"url": spec["url"], "destination": job["dest"],
                                                     "display_name": spec["name"]})
                ids[task["id"]] = spec["name"]
                service.dispatch("download_task", {"id": task["id"]})
        emit("submitted", tasks=len(ids))
        pending, ok = set(ids), True
        while pending:
            time.sleep(0.1)
            for task_id in list(pending):
                task = service.store.get(task_id)
                if task is None or task.state not in TERMINAL:
                    continue
                pending.discard(task_id)
                if task.state == "completed":
                    emit("file_done", name=ids[task_id], detail="engine")
                else:
                    ok = False
                    emit("file_error", name=ids[task_id], message=f"{task.state}: {task.error}"[:500])
        return ok
    finally:
        service.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--mode", choices=("core", "engine"), default="core")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    emit("hello", mode=args.mode, pid=os.getpid(), repo=str(REPO))
    try:
        if args.mode == "core":
            ok = run_core(job, args.resume)
        else:
            ok = run_engine(job, Path(args.data_dir or Path(job["dest"]).parent / "_mossdl_data"), args.resume)
    except Exception as exc:
        emit("error", message=f"{type(exc).__name__}: {exc}"[:1000])
        return 2
    emit("done", ok=ok)
    # Stay alive (like the real app) until the harness has taken its final
    # resource sample; it then sends "exit" or closes stdin.
    sys.stdin.readline()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
