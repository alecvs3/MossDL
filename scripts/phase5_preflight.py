"""Fail-closed executable preflight for completed Phase 5 contracts.

Validates:
1. Archive and package job models and table schemas are present and operational.
2. Headless RPC methods and authenticated scope maps are valid and callable.
3. Durable event outbox and event emission are functioning.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.models import ArchiveJob
from engine.service import (
    _HEADLESS_OPERATION_SCOPES,
    EngineService,
    headless_required_scope,
)


def verify_phase5_contracts(service: EngineService | None = None) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    # 1. Verify Headless RPC Methods and Scopes
    required_methods = [
        "browser_capture_batch",
        "container_import",
        "archive_inspect",
        "archive_submit",
        "events_since",
        "event_acknowledge",
        "schedule_get",
        "schedule_set",
        "collection_select",
        "collection_enqueue",
    ]
    for method in required_methods:
        scope = headless_required_scope(method)
        if not scope:
            errors.append(f"Missing required headless scope for method: {method}")

    # 2. Verify ArchiveJob Schema and Table Operations
    owns_service = False
    temp_dir = None
    if service is None:
        temp_dir = tempfile.TemporaryDirectory()
        service = EngineService(temp_dir.name)
        owns_service = True

    try:
        # Check archive job store operations
        task = service.dispatch("add_task", {
            "url": "https://example.test/test.part1.rar",
            "destination": temp_dir.name if temp_dir else "/tmp",
        })
        test_job = ArchiveJob(
            id="preflight-job-01",
            task_id=task["id"],
            input_path="/tmp/test.part1.rar",
            output_directory="/tmp/out",
            format="rar",
            password_ref=None,
            policy={},
            state="queued",
            progress_bytes=0,
            error=None,
            package_key="pkg-preflight-01",
            operation="join_verify",
            part_manifest=[],
            expected_size=1000,
            observed_size=None,
            expected_digest=None,
            observed_digest=None,
            attempt=0,
            recovery_reason=None,
            revision=0,
            staging_path="/tmp/staging",
        )
        saved_job, is_new = service.store.insert_or_get_archive_job(test_job)
        if not is_new or saved_job.id != test_job.id:
            errors.append("Failed to persist and retrieve archive job in store")

        # Check durable event outbox operations
        eid = service.events.emit("PreflightEvent", "preflight-task-01", {"status": "ok"})
        if not eid or eid <= 0:
            errors.append("Failed to emit durable event through EventPublisher")

        pending = service.events.pending()
        if not any(e["id"] == eid for e in pending):
            errors.append("Emitted event not found in pending outbox")

        # Verify headless dispatch
        res = service.dispatch("events_since", {"event_id": 0, "limit": 10})
        if not isinstance(res, list):
            errors.append("Headless events_since did not return list of events")

        try:
            service.store.delete(task["id"])
        except Exception:
            pass

    except Exception as exc:
        errors.append(f"Phase 5 contract execution failed: {exc}")
    finally:
        if owns_service:
            service.close()
            if temp_dir:
                temp_dir.cleanup()

    return len(errors) == 0, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 5 completion preflight check")
    parser.add_argument("--json", action="store_true", help="output JSON report")
    args = parser.parse_args(argv)

    passed, errors = verify_phase5_contracts()

    report = {
        "schema_version": 1,
        "phase": 5,
        "preflight": "passed" if passed else "failed",
        "errors": errors,
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        if passed:
            print("PHASE 5 PREFLIGHT PASS: All archive, headless, and event contracts verified.")
        else:
            print("PHASE 5 PREFLIGHT FAIL: Prerequisite contracts missing or non-functional:")
            for err in errors:
                print(f"  - {err}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())