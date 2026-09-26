/**
 * UI projection replay test.
 *
 * agents.md cardinal rule: "Engine is the sole source of truth for download
 * state, speeds, and I/O. The UI observes; it never simulates engine state."
 *
 * `tests/multipart_ui_state.test.ts` exercises the same projection functions
 * with hand-built fixtures -- which only proves the projections behave on states
 * a human imagined. This file replays states the ENGINE ACTUALLY PRODUCED,
 * captured from real runs (`reports/live/**`) and the deterministic offline
 * lifecycle matrix (`.test-artifacts/lifecycle-matrix/**`) and frozen into
 * `tests/fixtures/ui_snapshots/*.ui_snapshots.jsonl` by
 * `tests/live/ui_snapshots.py`.
 *
 * Every assertion below runs the REAL exported projection functions from
 * `src/figma/types.ts` / `src/figma/liveAdapters.ts` / `src/lib/multipart.ts`.
 * No mapping logic is reimplemented here: a test that duplicates production
 * logic proves nothing. The expected values come from the `engine_truth` block
 * each snapshot carries, which the Python replayer derives independently from
 * the engine's own telemetry (state-change and challenge records), never from
 * the projections under test.
 */

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { partitionMultiPartItems } from "../src/lib/multipart.ts";
import { taskToFigmaDownload } from "../src/figma/liveAdapters.ts";
import {
  aggregatePackageStatus,
  collectPendingChallenges,
  downloadBucket,
  isArchiveDrivenStage,
  isArchiveStage,
  isGenuinelyDownloading,
  packageBucket,
  packageProgress,
  quotaSignal,
  stageCountdownRemaining,
} from "../src/figma/types.ts";
import type { CaptchaChallenge, Task } from "../src/api.ts";

const FIXTURE_DIR = fileURLToPath(new URL("./fixtures/ui_snapshots/", import.meta.url));

interface EngineTruth {
  open_challenge_ids: string[];
  needs_user_task_ids: string[];
  downloading_task_ids: string[];
  bytes_known_task_ids: string[];
  archive: {
    task_id: string;
    event: string | null;
    state: string | null;
    stage: string;
    expected_size: number | null;
    progress_bytes: number | null;
    observed_size: number | null;
  } | null;
}

interface Snapshot {
  kind: "snapshot";
  run: string;
  seq: number;
  at: number;
  timestamp: string;
  trigger: string;
  tasks: Task[];
  captcha_pending: CaptchaChallenge[];
  engine_truth: EngineTruth;
}

interface RunMeta {
  kind: "meta";
  run: string;
  source: string;
  snapshot_count: number;
  final_states: Record<string, string>;
  stage_sequences: Record<string, string[]>;
  archive_jobs: Array<Record<string, unknown>>;
  distinct_challenge_count: number | null;
  timer_observations: Array<{
    run: string;
    task_id: string;
    at: number;
    engine_remaining_seconds: number;
    stage: string;
    stage_detail: Record<string, unknown>;
    stage_entered_at: number | null;
  }>;
}

interface Run {
  meta: RunMeta;
  snapshots: Snapshot[];
}

// ---------------------------------------------------------------- harness

const failures: string[] = [];
let checks = 0;

/** Records a violation instead of aborting, so one run's bug never hides another's. */
function expect(condition: unknown, label: string, evidence: () => string): void {
  checks += 1;
  if (!condition) failures.push(`${label}\n      evidence: ${evidence()}`);
}

function group(name: string, body: () => void): void {
  const before = failures.length;
  body();
  const added = failures.length - before;
  if (added === 0) console.log(`  [PASS] ${name}`);
  else console.log(`  [FAIL] ${name} (${added} violation${added === 1 ? "" : "s"})`);
}

function loadRuns(): Run[] {
  const files = readdirSync(FIXTURE_DIR).filter((name) => name.endsWith(".ui_snapshots.jsonl")).sort();
  assert.ok(files.length > 0, "No ui_snapshots fixtures found; run tests/live/ui_snapshots.py");
  return files.map((file) => {
    const rows = readFileSync(FIXTURE_DIR + file, "utf8")
      .split("\n")
      .filter((line) => line.trim().length > 0)
      .map((line) => JSON.parse(line) as RunMeta | Snapshot);
    const meta = rows[0];
    assert.equal(meta.kind, "meta", `${file} must start with a meta row`);
    return { meta: meta as RunMeta, snapshots: rows.slice(1) as Snapshot[] };
  });
}

const runs = loadRuns();
const project = (snapshot: Snapshot) => snapshot.tasks.map((task) => taskToFigmaDownload(task));
const at = (run: Run, snapshot: Snapshot) => `${run.meta.source} seq=${snapshot.seq} (${snapshot.trigger})`;

console.log("--- Running UI Projection Replay Tests ---");
console.log(
  `  replaying ${runs.reduce((n, r) => n + r.snapshots.length, 0)} engine snapshots from ${runs.length} captured runs:`
);
for (const run of runs) console.log(`    - ${run.meta.source} (${run.snapshots.length} snapshots)`);

// ------------------------------------------------- 1. projection totality

group("Every engine state the runs produced projects to a real view model", () => {
  const seenStates = new Set<string>();
  const seenStages = new Set<string>();
  for (const run of runs) {
    for (const snapshot of run.snapshots) {
      for (const task of snapshot.tasks) {
        seenStates.add(task.state);
        if (task.stage) seenStages.add(task.stage);
        const d = taskToFigmaDownload(task);
        expect(
          typeof d.detailedStatus === "string" && d.detailedStatus.length > 0,
          "Projection produced an empty status label",
          () => `${at(run, snapshot)} task=${task.id} state=${task.state} stage=${task.stage}`
        );
        expect(
          !/undefined|NaN|\[object/.test(d.detailedStatus || ""),
          "Projection leaked a JS artifact into the status label",
          () => `${at(run, snapshot)} detailedStatus=${JSON.stringify(d.detailedStatus)}`
        );
      }
    }
  }
  console.log(`         engine states replayed: ${[...seenStates].sort().join(", ")}`);
  console.log(`         engine stages replayed: ${[...seenStages].sort().join(", ")}`);
});

// -------------------------- 2. archive phase must never render as terminal

group("A package whose archive job is running never renders Completed or Waiting", () => {
  for (const run of runs) {
    for (const snapshot of run.snapshots) {
      const archive = snapshot.engine_truth.archive;
      // Engine truth: the archive job is still doing work.
      if (!archive || !["queued", "running", "verifying", "cleanup_pending"].includes(archive.state || "")) continue;
      const downloads = project(snapshot);
      const carrier = downloads.find((d) => d.id === archive.task_id);
      if (!carrier) continue;
      const { packages } = partitionMultiPartItems(downloads);
      const pkg = packages.find((p) => p.items.some((item) => item.id === archive.task_id));
      expect(
        pkg !== undefined,
        "Archive carrier is not grouped into any package",
        () => `${at(run, snapshot)} carrier=${archive.task_id} name=${carrier.name}`
      );
      if (!pkg) continue;
      const bucket = packageBucket(pkg.items);
      const status = aggregatePackageStatus(pkg.items);
      expect(
        bucket === "transferring",
        `Package bucketed as "${bucket}" while the engine archive job is ${archive.state}`,
        () =>
          `${at(run, snapshot)} bucket=${bucket} status=${status} ` +
          `parts=${pkg.items.map((i) => `${i.rawState}/${i.stage ?? "-"}`).join(" ")}`
      );
      expect(
        status === "downloading",
        `aggregatePackageStatus returned "${status}" during archive work`,
        () => `${at(run, snapshot)} archive=${archive.stage}/${archive.state}`
      );
      expect(
        downloadBucket(carrier) === "transferring",
        "Archive carrier row bucketed outside Transferring",
        () => `${at(run, snapshot)} bucket=${downloadBucket(carrier)} stage=${carrier.stage} state=${carrier.rawState}`
      );
      expect(
        carrier.detailedStatus !== "Completed" && carrier.detailedStatus !== "Starting",
        `Archive carrier badge reads "${carrier.detailedStatus}" while extracting`,
        () => `${at(run, snapshot)} stage=${carrier.stage} rawState=${carrier.rawState}`
      );
      // DownloadPackageRow.tsx:76-79 decides `archiveTask` (and therefore
      // `packageComplete`, the header badge and the stage carrier) with the same
      // predicate. If the engine drives a stage from the archive job that this
      // predicate does not recognise, the header falls through to
      // "All Parts Completed" while the archive job is still working.
      expect(
        isArchiveDrivenStage(carrier) || carrier.rawState === "postprocessing",
        "Engine drove an archive stage the UI's archive predicate does not recognise " +
          "(package header falls through to \"All Parts Completed\")",
        () =>
          `${at(run, snapshot)} carrierStage=${carrier.stage} carrierState=${carrier.rawState} ` +
          `archiveEvent=${archive.event} archiveState=${archive.state} ` +
          `-- see src/figma/types.ts:246 ARCHIVE_STAGES and src/figma/components/DownloadPackageRow.tsx:76`
      );
    }
  }
});

group("The archive-stage taxonomy has exactly one definition", () => {
  // A second, hardcoded copy of the archive stage list is how the header
  // predicate drifted away from the stages the engine actually drives.
  const headerSource = readFileSync(new URL("../src/figma/components/DownloadPackageRow.tsx", import.meta.url), "utf8");
  expect(
    !/\["unraring_pending",\s*"unraring_extracting",\s*"archive_cleanup"\]/.test(headerSource),
    "DownloadPackageRow re-implements the archive-stage list instead of calling isArchiveStage",
    () => "src/figma/components/DownloadPackageRow.tsx:76 duplicates src/figma/types.ts:246 ARCHIVE_STAGES"
  );
});

// ------------------------------ 3/4. archive progress: carried, not frozen

interface PackageSample {
  seq: number;
  trigger: string;
  percent: number;
  stageCarrier: boolean;
  archiveState: string | null;
  archiveOperation: string | null;
  engineProgressBytes: number | null;
  bytesKnown: boolean;
  partCount: number;
}

function packageSeries(run: Run): PackageSample[] {
  const samples: PackageSample[] = [];
  for (const snapshot of run.snapshots) {
    const downloads = project(snapshot);
    const { packages } = partitionMultiPartItems(downloads);
    if (packages.length !== 1) continue;
    const pkg = packages[0];
    const progress = packageProgress(pkg.items);
    samples.push({
      seq: snapshot.seq,
      trigger: snapshot.trigger,
      percent: progress.percent,
      stageCarrier: progress.stageCarrier,
      archiveState: snapshot.engine_truth.archive?.state ?? null,
      archiveOperation: snapshot.engine_truth.archive?.operation ?? null,
      engineProgressBytes: snapshot.engine_truth.archive?.progress_bytes ?? null,
      bytesKnown: pkg.items.every((item) => snapshot.engine_truth.bytes_known_task_ids.includes(item.id)),
      partCount: pkg.items.length,
    });
  }
  return samples;
}

group("Package progress follows the engine's archive bytes wherever they are comparable", () => {
  // The engine's archive `expected_size` is the sum of the INPUT volumes (built
  // from the part manifest, engine/service.py). Whether that is a valid
  // denominator depends on the operation:
  //
  //   join_verify -- the output is a concatenation of the inputs, so the engine
  //                  asserts observed == expected. The ratio is real and the bar
  //                  must follow it.
  //   extract     -- the reported bytes are EXTRACTED OUTPUT, an unrelated
  //                  quantity, and the engine reports no incremental output
  //                  progress during extraction at all. There is no honest ratio
  //                  to draw, so the bar must hold at the download total and the
  //                  phase must be carried by the stage label instead (asserted
  //                  in the archive-bucketing group above).
  //
  // Showing `output_bytes / input_bytes` was the original defect: it rendered a
  // fully-downloaded 10 GB package at 0% for the whole extraction.
  for (const run of runs) {
    const series = packageSeries(run).filter((s) =>
      ["queued", "running", "verifying", "cleanup_pending"].includes(s.archiveState || "")
    );
    if (series.length === 0) continue;
    const operation = series.find((s) => s.archiveOperation)?.archiveOperation ?? null;
    const comparable = operation !== null && operation !== "extract";

    for (const sample of series) {
      if (comparable) {
        expect(
          sample.stageCarrier,
          "Package bar ignored the engine's archive stage_detail on a comparable-bytes operation",
          () =>
            `${run.meta.source} seq=${sample.seq} op=${operation} ` +
            `archive=${sample.archiveState} percent=${sample.percent.toFixed(2)}`
        );
        continue;
      }
      expect(
        !sample.stageCarrier,
        "Package bar drew a ratio from the engine's incomparable extract bytes " +
          "(output bytes over input bytes)",
        () =>
          `${run.meta.source} seq=${sample.seq} op=${operation} ` +
          `archive=${sample.archiveState} percent=${sample.percent.toFixed(2)}`
      );
      // The bar must hold at what the transport actually achieved -- never
      // collapse toward zero because a new phase started.
      if (sample.bytesKnown) {
        expect(
          sample.percent > 99.99,
          "Package bar left the download total once the archive phase started",
          () =>
            `${run.meta.source} seq=${sample.seq} archive=${sample.archiveState} ` +
            `percent=${sample.percent.toFixed(2)} (every part fully downloaded)`
        );
      }
    }

    const distinct = new Set(series.map((s) => s.percent.toFixed(4)));
    console.log(
      `         (${run.meta.run}: op=${operation}, ` +
        `${comparable ? "byte-driven" : "stage-driven"} archive bar, ` +
        `percents=${[...distinct].join(",")}, ` +
        `engineBytes=${series.map((s) => s.engineProgressBytes).join("->")})`
    );
  }
});

group("Package progress never runs backwards", () => {
  for (const run of runs) {
    const series = packageSeries(run);
    for (let i = 1; i < series.length; i += 1) {
      const previous = series[i - 1];
      const current = series[i];
      // completed_bytes is only engine-pinned once a part leaves the transfer,
      // so compare only where both sides are engine-known truth.
      if (!previous.bytesKnown || !current.bytesKnown) continue;
      expect(
        current.percent >= previous.percent - 0.0001,
        `Package bar regressed ${previous.percent.toFixed(2)}% -> ${current.percent.toFixed(2)}%`,
        () =>
          `${run.meta.source} seq ${previous.seq} (${previous.trigger}) -> ${current.seq} (${current.trigger}); ` +
          `archive=${current.archiveState} engineArchiveBytes=${current.engineProgressBytes} ` +
          `stageCarrier=${current.stageCarrier}`
      );
    }
  }
});

// ---------------------------------------------- 5. CAPTCHA banner counting

group("CAPTCHA banner count equals the DISTINCT engine challenges, not the needs_user sibling count", () => {
  let asymmetric = 0;
  for (const run of runs) {
    for (const snapshot of run.snapshots) {
      const downloads = project(snapshot);
      const collected = collectPendingChallenges(snapshot.captcha_pending, downloads);
      const truth = snapshot.engine_truth.open_challenge_ids;
      const needsUser = snapshot.engine_truth.needs_user_task_ids.length;
      if (needsUser !== truth.length) asymmetric += 1;
      expect(
        collected.length === truth.length,
        `Banner would show ${collected.length} challenge(s); the engine has ${truth.length} open`,
        () =>
          `${at(run, snapshot)} collected=${JSON.stringify(collected.map((c) => c.id))} ` +
          `engine=${JSON.stringify(truth)} needs_user=${JSON.stringify(snapshot.engine_truth.needs_user_task_ids)}`
      );
      expect(
        collected.every((c) => truth.includes(c.id)),
        "Banner surfaced a challenge id the engine does not have open (fabricated or stale)",
        () => `${at(run, snapshot)} collected=${JSON.stringify(collected.map((c) => c.id))} engine=${JSON.stringify(truth)}`
      );
      // The historical bug: max(captchaPending.length, needs_user count).
      const legacyCount = Math.max(snapshot.captcha_pending.length, needsUser);
      expect(
        legacyCount >= collected.length,
        "Sanity: dedupe must never inflate the legacy count",
        () => `${at(run, snapshot)} legacy=${legacyCount} collected=${collected.length}`
      );
    }
  }
  console.log(`         snapshots where needs_user count != distinct engine challenges: ${asymmetric}`);
});

// --------------------------------------- 6. no fabricated speed/ETA values

const ETA_SHAPE = /^(—|\d+s|\d+m \d+s)$/;

group("Speed/ETA are shown only where the engine says the task is genuinely downloading", () => {
  for (const run of runs) {
    for (const snapshot of run.snapshots) {
      const truth = new Set(snapshot.engine_truth.downloading_task_ids);
      for (const d of project(snapshot)) {
        expect(
          isGenuinelyDownloading(d) === truth.has(d.id),
          "isGenuinelyDownloading disagrees with the engine's own state log",
          () =>
            `${at(run, snapshot)} task=${d.id} projected=${isGenuinelyDownloading(d)} ` +
            `engineDownloading=${truth.has(d.id)} rawState=${d.rawState} stage=${d.stage}`
        );
        expect(
          Number.isFinite(d.speed) && d.speed >= 0,
          "Projected speed is not a finite non-negative number",
          () => `${at(run, snapshot)} task=${d.id} speed=${d.speed}`
        );
        expect(
          ETA_SHAPE.test(d.eta),
          `Projected ETA "${d.eta}" is not an engine-derived duration or the blank marker`,
          () => `${at(run, snapshot)} task=${d.id} rawState=${d.rawState}`
        );
        expect(
          !(d.eta !== "—" && !isGenuinelyDownloading(d)),
          "A concrete ETA is projected for a task the engine is not downloading",
          () => `${at(run, snapshot)} task=${d.id} eta=${d.eta} rawState=${d.rawState}`
        );
      }
    }
  }
});

group("No rendered field can produce a doubled speed unit or a status-as-speed label", () => {
  // The historical "0.00 MB/s/s" and "Queued" speed labels came from the render
  // sites concatenating "/s" onto an already-suffixed formatter, and from using
  // the status text in the speed column. Guard the real call sites.
  const renderSites = [
    "src/figma/pages/DownloadsPage.tsx",
    "src/figma/components/DownloadPackageRow.tsx",
    "src/figma/components/DownloadSheet.tsx",
    "src/figma/ui/GroupHeader.tsx",
    "src/figma/ui/Sparkline.tsx",
  ];
  for (const site of renderSites) {
    const source = readFileSync(new URL(`../${site}`, import.meta.url), "utf8");
    expect(
      !/formatSpeed\([^)]*\)\}\/s/.test(source),
      `${site} concatenates "/s" onto formatSpeed (yields "0.00 MB/s/s")`,
      () => site
    );
    expect(!source.includes("/s/s"), `${site} contains a literal doubled speed unit`, () => site);
    expect(
      !/\{[^}]*detailedStatus[^}]*\}\s*\/s/.test(source),
      `${site} renders a status label in the speed column`,
      () => site
    );
  }
  // Both per-row speed cells must be gated on the engine's genuine-download flag.
  const rowSource = readFileSync(new URL("../src/figma/components/DownloadPackageRow.tsx", import.meta.url), "utf8");
  expect(
    rowSource.includes("isGenuinelyDownloading(d) ? `${fmtBytes(d.speed)}/s`"),
    "Package part speed cell is not gated on isGenuinelyDownloading",
    () => "src/figma/components/DownloadPackageRow.tsx"
  );
});

// ------------------------------------------------ 7. quota banner gating

group("Quota banner never fires on a plain engine failure", () => {
  for (const run of runs) {
    for (const snapshot of run.snapshots) {
      for (const d of project(snapshot)) {
        // None of the replayed runs produced a quota signal; every engine error
        // in them (checksum mismatch, solver timeout, provider rejection) must
        // stay silent. The historical bug fired on ANY status === "error".
        expect(
          quotaSignal(d) === null,
          `Quota banner fired on a non-quota engine state ("${quotaSignal(d)}")`,
          () => `${at(run, snapshot)} task=${d.id} state=${d.rawState} error=${JSON.stringify(d.error)}`
        );
      }
    }
  }
  const failed = runs.flatMap((run) =>
    run.snapshots.flatMap((snapshot) =>
      project(snapshot)
        .filter((d) => d.status === "error")
        .map((d) => `${run.meta.run}:${d.id}`)
    )
  );
  console.log(`         error-state rows replayed with the banner suppressed: ${new Set(failed).size}`);
});

// ------------------------------------------- 8. live wait-timer countdown

group("Wait-timer countdown tracks the engine's own TIMER_TICK stream", () => {
  let observed = 0;
  for (const run of runs) {
    for (const tick of run.meta.timer_observations) {
      observed += 1;
      const projected = stageCountdownRemaining(
        {
          stage: tick.stage,
          stageDetail: tick.stage_detail,
          stageEnteredAt: tick.stage_entered_at ?? undefined,
        },
        tick.at
      );
      expect(
        projected !== null,
        "No countdown projected while the engine was counting down",
        () => `${run.meta.source} task=${tick.task_id} at=${tick.at} detail=${JSON.stringify(tick.stage_detail)}`
      );
      // Telemetry timestamps are whole seconds, so allow one second of slack --
      // still far tighter than a projection that ignores the elapsed anchor.
      expect(
        projected !== null && Math.abs(projected - tick.engine_remaining_seconds) <= 1,
        `Countdown projected ${projected}s while the engine reported ${tick.engine_remaining_seconds}s`,
        () =>
          `${run.meta.source} task=${tick.task_id} at=${tick.at} ` +
          `anchor=${tick.stage_detail.countdown_captured_at ?? tick.stage_entered_at} ` +
          `reported=${tick.stage_detail.countdown_seconds}`
      );
    }
  }
  console.log(`         engine countdown ticks cross-checked: ${observed}`);
  assert.ok(observed > 0, "Fixtures contain no hoster_wait_timer ticks to cross-check");
});

// -------------------------------------- 9. integrity badge requires both

group("Verified integrity badge requires hash verified AND size ok", () => {
  const source = readFileSync(new URL("../src/figma/components/DownloadSheet.tsx", import.meta.url), "utf8");
  const greenBranch = Math.max(source.indexOf('"var(--success)"'), source.indexOf('"#34d399"'));
  const sizeGate = source.indexOf("sha512State.sizeOk === false");
  expect(
    sizeGate !== -1 && greenBranch !== -1 && sizeGate < greenBranch,
    "The green verified colour is not gated behind a size_ok check",
    () => `sizeOkGate@${sizeGate} greenBranch@${greenBranch} in src/figma/components/DownloadSheet.tsx`
  );
  expect(
    source.includes('sha512State.state === "verified"'),
    "The green verified colour does not require the engine's verified state",
    () => "src/figma/components/DownloadSheet.tsx"
  );
  expect(
    source.includes("typeof res.size_ok === \"boolean\""),
    "The UI ignores the engine's explicit size_ok field",
    () => "src/figma/components/DownloadSheet.tsx"
  );
});

// --------------------------------------------- 10. terminal state fidelity

group("Final rendered state matches the engine's recorded final state", () => {
  const TERMINAL: Record<string, string> = {
    completed: "completed",
    failed: "failed",
    canceled: "failed",
    needs_user: "waiting",
  };
  for (const run of runs) {
    const last = run.snapshots[run.snapshots.length - 1];
    const downloads = project(last);
    for (const [taskId, finalState] of Object.entries(run.meta.final_states || {})) {
      const d = downloads.find((item) => item.id === taskId);
      if (!d) continue;
      const expectedBucket = TERMINAL[finalState];
      if (!expectedBucket) continue;
      // A task the engine still has in an archive stage is legitimately active.
      if (isArchiveStage(d.stage)) continue;
      expect(
        downloadBucket(d) === expectedBucket,
        `Final row bucketed "${downloadBucket(d)}" for engine final state "${finalState}"`,
        () => `${run.meta.source} task=${taskId} rawState=${d.rawState} stage=${d.stage} label=${d.detailedStatus}`
      );
    }
  }
});

// ------------------------------------------------------------------ report

console.log("");
console.log(`  ${checks} projection assertions evaluated across ${runs.length} captured engine runs.`);
if (failures.length > 0) {
  console.log("");
  console.log(`  ${failures.length} PROJECTION MISMATCH(ES) -- the UI contradicts the engine:`);
  const unique = new Map<string, string[]>();
  for (const failure of failures) {
    const [message, ...rest] = failure.split("\n");
    const bucket = unique.get(message) || [];
    bucket.push(rest.join("\n"));
    unique.set(message, bucket);
  }
  for (const [message, evidence] of unique) {
    console.log(`\n  - ${message}  [${evidence.length} occurrence(s)]`);
    for (const line of evidence) console.log(line);
  }
  console.log("");
  throw new Error(`${failures.length} UI projection mismatch(es) against real engine state`);
}
console.log("All UI Projection Replay Tests Passed Successfully!");
