# AGENTS.md — Core Engineering Constraints

## Architectural Boundaries
- Engine (Tier 2) is the sole source of truth for download state, speeds, and I/O. The UI observes; it never simulates engine state.
- Desktop Shell (Tier 1) owns window borders, dragging, and native IPC. Do not emulate native window frames in CSS.
- Provider data contracts are public and backward-compatible (append-only; preserve hierarchical tree nodes).

## Strict Prohibitions
1. No Ad-Hoc UI Selection: Follow standard desktop conventions (`selectedIds`, `focusedId`, `anchorId`). Do not invent `isHighlighted` or bypass the selection machine.
2. No Engine Simulation: Never mock or duplicate download states/progress inside the UI layer.
3. No Symptom Hiding: Do not use empty `.catch()` blocks, arbitrary `setTimeout`s, or blanket CSS `user-select: none` on inputs/selectable text.
4. Shell Dragging: Always mark non-draggable elements inside draggable titlebars with proper IPC non-drag regions.
5. No Silent Cascade Bails: Decision cascades (solvers, backends, route fallbacks) must never silently skip candidates via bare `continue` or `return None`. Always log the disqualifying condition in structured telemetry.

## Verification
- Before completing: run typechecker (`tsc`) and relevant test suite. Zero errors allowed.
- Ensure no shared provider interface changes break existing consumers.

## AI Ergonomics & Anti-Bloat Philosophy (Zero Token Waste)
1. **Single Source of Truth**: Never duplicate a helper function (formatting, paths, errors). If it exists in `engine/utils.py` or `src/lib/format.ts`, reuse it.
2. **Small Modular Files**: Keep files focused and under ~300 lines wherever practical. Small files prevent context overflows, reduce syntax diff corruption, and make AI edits pinpoint accurate.
3. **Mechanical Guardrails Over Honor System**: Do not trust memory; rely on AST duplication scanners (`jscpd`) and dead code linters (`vulture`/`knip`) in `scripts/check_all.py`.
4. **Clean Root Topography**: No loose scratch scripts, logs, or unparented tests in the repository root. Everything has an explicit home.
5. **Ubiquitous Feature Completeness**: When adding or fixing a capability (e.g. multi-part package context actions, batch selections, state badges), apply it completely across all relevant actions, views, and states (delete, pause, resume, retry, copy URLs, folder actions) rather than patching only a single isolated branch.
6. **Targeted Code Exploration (No Endless Scanning)**: Rely on pinpoint tools (`grep_search`, `find_by_name`) with bounded queries rather than broad multi-directory traversals or sweeping file reads. Inspect only the precise modules participating in the workflow.
7. **Empirical Probing Over Passive Read Loops**: Never execute consecutive rounds of passive file inspection. If the root cause or control flow is not evident after 2 targeted reads, immediately switch to active instrumentation: write an ephemeral probe/test script in `scratch/` or run an isolated CLI command to reproduce, capture state transitions, and observe live runtime behavior directly.
8. **Zero-State Probing on Silent Stalls**: When the symptom is "nothing happens", "silent failure", or "empty logs", do not guess or read code branches. Write a 10-line probe in `scratch/` to print the candidate list, boolean config flags, and step through the cascade directly.
