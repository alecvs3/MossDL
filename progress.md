# Progress Log: First Public Release (v0.1.0) Preparation & Launch

## Session: 2026-09-26

- **Context Ingestion & Discovery**:
  - Ingested Claude's final trajectory: benchmarks passed, concurrency auditor isolated to `<data_dir>/logs`, `transfer_core/scheduler.rs` modularized, browser extensions packaged and submitted to Chrome Web Store and Firefox AMO.
  - Verified DNS resolution and HTTP status for `mossdownloader.com` (200 OK) and `mossdl.com` (301 Moved Permanently to `mossdownloader.com`).
  - Discovered pre-rendered graphics suite in `docs/brand/` (hero stills, feature cards, demo mp4/gifs, social preview).
  - Executed full quality gate `python scripts/check_all.py --profile fast`: 18/19 checks passed. Pinpointed single failing check (`contract-scanner`) to orphaned `load_persisted_profiles` in `engine/service.py`.
  - Verified `browser_extension` test suite (13/13 passed in 93ms).
  - Verified `website` build via Astro (6/6 static pages built in <1s, 0 errors).
  - Verified GitHub CLI auth (`alecvs3` authenticated with `repo` and `workflow` scopes).
- **Planning**:
  - Created `findings.md` dossier detailing extension IDs, domain configs, brand assets, and test matrix.
  - Initialized `task_plan.md` for comprehensive release execution across onboarding, website graphics, contract repair, git repository push, release packaging, and deployment.
- **Phase 1: Engine Quality Gate & Performance Fixes**:
  - Restored `concurrency_auditor.load_persisted_profiles()` in `engine/service.py` after `attach_store`.
  - Fixed loopback resolution delay: exempted loopback hosts (`127.0.0.1`, `localhost`, `::1`, `0.0.0.0`) from domain resolve locks and stagger delay in `engine/service.py` and `engine/resolve_stagger.py`.
  - Rebuilt `transfer-core` in release mode (`cargo build --manifest-path src-tauri/Cargo.toml --bin transfer-core --release`).
  - Cleared stale background processes and verified `test_rust_mega_transform.py` passes in 3.1s.
  - Verified benchmark smoke run: completion time plummeted from 13.6s to 1.96s (85% reduction in latency).
  - Executed `python scripts/check_all.py --profile fast`: 19/19 checks passed cleanly in 52.74s (zero failures).
