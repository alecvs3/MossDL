import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  detectMultiPart,
  normalizePackageName,
  getMultiPartFolder,
  partitionMultiPartItems,
} from "../src/lib/multipart.ts";
import {
  taskToFigmaDownload,
  resolveDestinationWithPackage,
} from "../src/figma/liveAdapters.ts";
import {
  aggregatePackageStatus,
  collectPendingChallenges,
  downloadBucket,
  packageBucket,
  packageProgress,
  packageStageCarrier,
  quotaSignal,
  stageCountdownRemaining,
} from "../src/figma/types.ts";
import { applyEngineEvents, extractArchiveChoices, extractIncompletePackages } from "../src/lib/engineEvents.ts";
import type { EventEnvelope, Task } from "../src/api.ts";

console.log("--- Running Multipart UI State Tests ---");

// 1. MultiPart Detection & Normalization
{
  const info1 = detectMultiPart("RuneScape_Dragonwilds_--_example-repacks.test_--_.part1.rar");
  assert.ok(info1, "Must detect part1.rar");
  assert.equal(info1.part, 1);
  assert.equal(info1.ext, "rar");

  const info7 = detectMultiPart("https://datanodes.to/3rycs4yr8ukd/RuneScape_Dragonwilds_--_example-repacks.test_--_.part7.rar");
  assert.ok(info7, "Must detect URL leaf part7.rar");
  assert.equal(info7.part, 7);

  const cleanPkg = normalizePackageName(info1.base);
  assert.equal(cleanPkg, "RuneScape Dragonwilds");

  const folder = getMultiPartFolder("RuneScape_Dragonwilds_--_example-repacks.test_--_.part1.rar");
  assert.equal(folder, "RuneScape Dragonwilds");
  console.log("  [PASS] Detection & Normalization");
}

// 2. Package Folder Resolution (Idempotent)
{
  const base = "N:\\dl's";
  const url = "https://datanodes.to/1/RuneScape_Dragonwilds_--_example-repacks.test_--_.part1.rar";
  const resolved = resolveDestinationWithPackage(base, url);
  assert.equal(resolved, "N:\\dl's\\RuneScape Dragonwilds");

  // Re-resolving already nested path must be idempotent
  const nested = resolveDestinationWithPackage(resolved, url);
  assert.equal(nested, "N:\\dl's\\RuneScape Dragonwilds");
  console.log("  [PASS] Idempotent Destination Resolution");
}

// 3. 12-State Matrix & Multipart Stage Badges
{
  const makeTask = (state: any, stage?: string, detail?: Record<string, unknown>): Task => ({
    id: "test-task-1",
    source_url: "https://datanodes.to/1/Game.part1.rar",
    destination: "N:\\dl's\\Game",
    display_name: "Game.part1.rar",
    completed_bytes: 1024 * 1024,
    size: 5 * 1024 * 1024,
    state,
    stage: stage || null,
    stage_detail: detail || {},
  });

  // Check canonical state transitions
  assert.equal(taskToFigmaDownload(makeTask("queued")).detailedStatus, "Starting");
  assert.equal(taskToFigmaDownload(makeTask("pending_probe")).detailedStatus, "Waiting for Part 1");
  assert.equal(taskToFigmaDownload(makeTask("resolving")).detailedStatus, "Getting metadata");
  assert.equal(taskToFigmaDownload(makeTask("preflight")).detailedStatus, "Preparing");
  assert.equal(taskToFigmaDownload(makeTask("downloading")).detailedStatus, "Downloading");
  assert.equal(taskToFigmaDownload(makeTask("verifying")).detailedStatus, "Verifying");
  assert.equal(taskToFigmaDownload(makeTask("postprocessing")).detailedStatus, "Extracting");
  assert.equal(taskToFigmaDownload(makeTask("completed")).detailedStatus, "Completed");
  assert.equal(taskToFigmaDownload(makeTask("paused")).detailedStatus, "Paused");
  assert.equal(taskToFigmaDownload(makeTask("retrying")).detailedStatus, "Retrying");
  assert.equal(taskToFigmaDownload(makeTask("failed")).detailedStatus, "Failed");
  assert.equal(taskToFigmaDownload(makeTask("canceled")).detailedStatus, "Cancelled");

  // Check stage overrides
  assert.equal(taskToFigmaDownload(makeTask("downloading", "unraring_pending")).detailedStatus, "Waiting for archive parts");
  assert.equal(taskToFigmaDownload(makeTask("postprocessing", "unraring_extracting")).detailedStatus, "Extracting");
  assert.equal(taskToFigmaDownload(makeTask("postprocessing", "archive_cleanup")).detailedStatus, "Cleaning up archives");
  assert.equal(taskToFigmaDownload(makeTask("verifying", "verifying_integrity")).detailedStatus, "Verifying");
  assert.equal(taskToFigmaDownload(makeTask("resolving", "hoster_wait_timer", { countdown_seconds: 15 })).detailedStatus, "Waiting timer (15s)...");
  assert.equal(taskToFigmaDownload(makeTask("resolving", "captcha_solving", { solver: "Clearcote" })).detailedStatus, "Solving CAPTCHA (Clearcote)");

  console.log("  [PASS] 12-State Matrix & Stage Badges");
}

// 4. Partitioning & Grouping
{
  const tasks = [1, 2, 3].map((n) => taskToFigmaDownload({
    id: `part-${n}`,
    source_url: `https://datanodes.to/${n}/Package.part${n}.rar`,
    destination: "N:\\dl's\\Package",
    display_name: `Package.part${n}.rar`,
    state: n === 1 ? "downloading" : "queued",
    completed_bytes: n === 1 ? 500 : 0,
    size: 1000,
  } as Task));

  const partitioned = partitionMultiPartItems(tasks);
  assert.equal(partitioned.packages.length, 1);
  assert.equal(partitioned.packages[0].name, "Package");
  assert.equal(partitioned.packages[0].items.length, 3);
  assert.equal(partitioned.standalone.length, 0);
  console.log("  [PASS] Partitioning & Grouping");
}

// 5. Package Lifecycle Aggregation
{
  const makeTask = (id: string, name: string, state: any, stage?: string): Task => ({
    id,
    source_url: `https://datanodes.to/${id}/${name}`,
    destination: "N:\\dl's\\Game",
    display_name: name,
    completed_bytes: 1000,
    size: 1000,
    state,
    stage: stage || null,
    stage_detail: {},
  });

  const part1 = taskToFigmaDownload(makeTask("p1", "Game.part1.rar", "completed", "unraring_extracting"));
  const part2 = taskToFigmaDownload(makeTask("p2", "Game.part2.rar", "completed"));
  const tasks = [part1, part2];

  const archiveTask = tasks.find(
    (t) => (t.stage && ["unraring_pending", "unraring_extracting", "archive_cleanup"].includes(t.stage)) || t.rawState === "postprocessing"
  );
  assert.ok(archiveTask, "Must locate the archive-extracting task in the package");
  assert.equal(archiveTask.stage, "unraring_extracting");
  assert.equal(archiveTask.detailedStatus, "Extracting");
  console.log("  [PASS] Package Lifecycle Aggregation");
}

// 6. Archive stages bucket as in-progress work
{
  const byteBase = 1024 * 1024;
  const part1 = taskToFigmaDownload({
    id: "arch-p1",
    source_url: "https://datanodes.to/1/Game.part1.rar",
    destination: "N:\\dl's\\Game",
    display_name: "Game.part1.rar",
    state: "completed",
    completed_bytes: byteBase,
    size: byteBase,
    stage: "unraring_extracting",
    stage_detail: { expected_size: byteBase * 2, progress_bytes: byteBase },
    stage_entered_at: 1_000_000,
  } as Task);
  const part2 = taskToFigmaDownload({
    id: "arch-p2",
    source_url: "https://datanodes.to/2/Game.part2.rar",
    destination: "N:\\dl's\\Game",
    display_name: "Game.part2.rar",
    state: "completed",
    completed_bytes: byteBase,
    size: byteBase,
  } as Task);

  assert.equal(aggregatePackageStatus([part1, part2]), "downloading");
  assert.equal(packageBucket([part1, part2]), "transferring");
  assert.equal(downloadBucket(part1), "transferring");

  const progress = packageProgress([part1, part2]);
  assert.equal(Math.round(progress.percent), 50, "Archive carrier drives the live package bar");
  assert.equal(Math.round(progress.totalMB), 2);
  assert.equal(progress.stageCarrier, true);
  assert.equal(part1.detailedStatus, "Extracting (50%)");

  const postprocessing = taskToFigmaDownload({
    id: "arch-p3",
    source_url: "https://datanodes.to/3/Game.part3.rar",
    destination: "N:\\dl's\\Game",
    display_name: "Game.part3.rar",
    state: "postprocessing",
    completed_bytes: byteBase,
    size: byteBase,
    stage: "archive_cleanup",
    stage_detail: { expected_size: byteBase, progress_bytes: byteBase },
  } as Task);
  assert.equal(postprocessing.status, "downloading", "postprocessing is active, not waiting");
  assert.equal(downloadBucket(postprocessing), "transferring");

  const finished = taskToFigmaDownload({
    id: "arch-p4",
    source_url: "https://datanodes.to/4/Game.part4.rar",
    destination: "N:\\dl's\\Game",
    display_name: "Game.part4.rar",
    state: "completed",
    completed_bytes: byteBase,
    size: byteBase,
    stage: "completed",
  } as Task);
  assert.equal(downloadBucket(finished), "completed");
  console.log("  [PASS] Archive Stage Bucketing & Live Aggregate Progress");
}

// 7. hoster_wait_timer countdown projects from stage_entered_at
{
  const now = 1_000_000;
  const waiting = {
    stage: "hoster_wait_timer" as const,
    stageDetail: { countdown_seconds: 15 },
    stageEnteredAt: now - 5,
  };
  assert.equal(stageCountdownRemaining(waiting, now), 10);
  assert.equal(stageCountdownRemaining({ ...waiting, stageEnteredAt: now - 60 }, now), 0, "Never negative");
  assert.equal(stageCountdownRemaining({ ...waiting, stageEnteredAt: undefined }, now), 15);
  assert.equal(stageCountdownRemaining({ ...waiting, stage: "downloading" as any }, now), null);
  console.log("  [PASS] Live Wait-Timer Countdown");
}

// 8. Quota banner only on real engine quota signals
{
  const task = (overrides: Partial<Task>): ReturnType<typeof taskToFigmaDownload> =>
    taskToFigmaDownload({
      id: "quota",
      source_url: "https://example.test/file.bin",
      destination: "N:\\dl's",
      display_name: "file.bin",
      state: "error",
      completed_bytes: 0,
      size: 1024,
      ...overrides,
    } as Task);

  assert.equal(quotaSignal(task({ error: "404 not found" })), null, "Generic errors never fire the quota banner");
  assert.equal(
    quotaSignal(task({ error: "server said quota exceeded somewhere in the body" })),
    null,
    "Non-canonical quota mentions are not engine signals"
  );
  const engineQuota = task({
    state: "needs_user",
    error: "Download quota reached: Add a Google account or switch VPN to retry",
  });
  assert.ok(quotaSignal(engineQuota)?.startsWith("Download quota reached"));
  const structured = task({ stage: "downloading", stage_detail: { reason: "quota_exceeded" } } as Partial<Task>);
  assert.equal(quotaSignal(structured), "quota_exceeded");
  console.log("  [PASS] Quota Signal Gating");
}

// 9. Exactly one real CAPTCHA challenge / CTA
{
  const challenge = {
    id: "chal-shared",
    task_id: "cap-p1",
    provider_id: "datanodes",
    captcha_type: "turnstile",
    params: {},
    timeout_seconds: 90,
    created_at: 0,
    expires_at: 0,
    time_remaining: 30,
    status: "pending",
    loopback_url: "http://127.0.0.1:9000/captcha/chal-shared",
  };
  const siblingTask = (id: string): ReturnType<typeof taskToFigmaDownload> =>
    taskToFigmaDownload({
      id,
      source_url: `https://datanodes.to/${id}/Game.part1.rar`,
      destination: "N:\\dl's\\Game",
      display_name: "Game.part1.rar",
      state: "needs_user",
      completed_bytes: 0,
      size: 1024,
      user_action: "turnstile",
      user_challenge: { ...challenge, task_id: id },
    } as Task);

  // Two needs_user siblings pointing at the same engine challenge must count once.
  const deduped = collectPendingChallenges([challenge], [siblingTask("cap-p1"), siblingTask("cap-p2")]);
  assert.equal(deduped.length, 1);
  assert.equal(deduped[0].id, "chal-shared");

  // Challenge metadata without a real engine id must never be synthesized.
  const noEngineId = collectPendingChallenges([], [
    taskToFigmaDownload({
      id: "cap-p3",
      source_url: "https://host.test/x",
      destination: "N:\\dl's",
      display_name: "x",
      state: "needs_user",
      completed_bytes: 0,
      size: 1,
      user_action: "turnstile",
      user_challenge: { captcha_type: "turnstile" },
    } as Task),
  ]);
  assert.equal(noEngineId.length, 0);

  // One measurable CTA: the banner routes through onCaptchaSolve exactly once,
  // and package/row rows no longer call the solver directly.
  const downloadsSource = readFileSync(new URL("../src/figma/pages/DownloadsPage.tsx", import.meta.url), "utf8");
  assert.equal((downloadsSource.match(/onCaptchaSolve\?\.\(/g) || []).length, 1);
  const packageSource = readFileSync(
    new URL("../src/figma/components/DownloadPackageRow.tsx", import.meta.url),
    "utf8"
  );
  assert.ok(!packageSource.includes("solveCaptcha("), "Package row must not trigger CAPTCHA solves");
  assert.ok(!packageSource.includes("solveMultipartCaptcha("), "Package row must not trigger multipart solves");
  assert.ok(!packageSource.includes("CAPTCHA!"), "Package row must not render duplicate CAPTCHA CTAs");
  console.log("  [PASS] Single CAPTCHA Challenge & CTA");
}

// 10. Engine event deltas are idempotent and revision-guarded
{
  const task = {
    id: "evt-1",
    source_url: "https://datanodes.to/1/Game.part1.rar",
    destination: "N:\\dl's\\Game",
    display_name: "Game.part1.rar",
    completed_bytes: 0,
    size: 2048,
    state: "completed",
    revision: 4,
  } as Task;

  const stageEvent: EventEnvelope = {
    id: 1,
    event_id: "1",
    event_type: "TaskStageChanged",
    task_id: "evt-1",
    payload: {
      stage: "unraring_extracting",
      stage_detail: { expected_size: 4096, progress_bytes: 1024 },
      stage_entered_at: 1234.5,
      revision: 5,
    },
  };
  const applied = applyEngineEvents([task], [stageEvent]);
  assert.equal(applied[0].stage, "unraring_extracting");
  assert.equal(applied[0].stage_detail?.progress_bytes, 1024);

  // A stale payload (revision behind the snapshot) must be dropped, not applied.
  const stale = applyEngineEvents(
    [{ ...task, revision: 9, stage: "downloading" }],
    [stageEvent]
  );
  assert.equal(stale[0].stage, "downloading");

  // Archive choice events surface the durable engine decision.
  const choices = extractArchiveChoices([
    {
      id: 2,
      event_id: "2",
      event_type: "ArchiveChoiceRequired",
      task_id: "evt-1",
      payload: {
        task_id: "evt-1",
        input_path: "N:\\dl's\\Game\\Game.part1.rar",
        output_directory: "N:\\dl's\\Game",
        expected_size: 4096,
        package_key: "pkg-1",
      },
    },
  ]);
  assert.equal(choices.length, 1);
  assert.equal(choices[0].packageKey, "pkg-1");
  console.log("  [PASS] Engine Event Delta Application");
}

// 11. Package header carrier: a running countdown must not be hidden by a transfer
{
  const part = (id: string, over: Partial<any> = {}): any => ({
    id,
    name: `Game.part${id}.rar`,
    status: "downloading",
    rawState: "resolving",
    size: 2000,
    downloaded: 0,
    speed: 0,
    stage: undefined,
    stageDetail: {},
    stageEnteredAt: undefined,
    ...over,
  });

  // A sibling counting down while part 1 transfers: the countdown is transient
  // and invisible on a collapsed package, so it owns the header badge.
  const carrier = packageStageCarrier([
    part("1", { rawState: "downloading", status: "downloading", stage: "downloading", speed: 50 }),
    part("2", {
      rawState: "needs_user",
      stage: "hoster_wait_timer",
      stageDetail: { countdown_seconds: 9, countdown_captured_at: Date.now() / 1000 },
      stageEnteredAt: Date.now() / 1000,
    }),
  ]);
  assert.equal(carrier?.id, "2", "an active transfer hid a sibling's live countdown");
  assert.equal(
    stageCountdownRemaining(carrier as any, Date.now() / 1000 + 2), 7,
    "header carrier did not project a live countdown");

  // With no timer running, the active transfer still wins.
  const transferring = packageStageCarrier([
    part("1", { rawState: "downloading", status: "downloading", stage: "downloading", speed: 50 }),
    part("2", { rawState: "queued", stage: undefined }),
  ]);
  assert.equal(transferring?.id, "1");

  // Archive work is still surfaced once transfers are done.
  const archiving = packageStageCarrier([
    part("1", { rawState: "completed", status: "completed", stage: "unraring_extracting" }),
  ]);
  assert.equal(archiving?.id, "1");

  assert.equal(packageStageCarrier([]), undefined);
  console.log("  [PASS] Package Header Stage Carrier");
}

// 12. An incomplete package must be surfaced, not silently stamped on a task
{
  const events: EventEnvelope[] = [
    {
      event_id: "10",
      event_type: "ArchivePackageIncomplete",
      task_id: "inc-1",
      payload: {
        task_id: "inc-1",
        package: "Game_--_example-repacks.test_--_",
        found_parts: [1, 2, 4],
        missing_parts: [3],
        directory: "N:\\dl's\\Game",
      },
    } as any,
  ];

  const incomplete = extractIncompletePackages(events);
  assert.equal(incomplete.length, 1, "the user is never told the package is unusable");
  assert.deepEqual(incomplete[0].missingParts, [3]);
  assert.deepEqual(incomplete[0].foundParts, [1, 2, 4]);

  // It must NOT be treated as a report from a running archive job: doing so
  // stamps stage_detail.source="archive" onto a task whose job never ran.
  const task: Task = {
    id: "inc-1",
    source_url: "https://datanodes.to/1/Game.part1.rar",
    destination: "N:\\dl's\\Game",
    display_name: "Game.part1.rar",
    completed_bytes: 10,
    size: 10,
    state: "completed",
    stage: "completed",
    stage_detail: {},
  } as any;
  const [applied] = applyEngineEvents([task], events);
  assert.equal(
    (applied.stage_detail as any)?.source, undefined,
    "an advisory event polluted the task's archive stage detail");
  assert.equal(applied.stage, "completed", "an advisory event moved the lifecycle stage");
  console.log("  [PASS] Incomplete Package Advisory");
}

// 13. Auto-solving sibling challenges must not become user prompts
{
  const part = (id: string, challenge: Record<string, unknown> | null): any => ({
    id,
    name: `Game.part${id}.rar`,
    status: "needs_user",
    rawState: "needs_user",
    user_challenge: challenge,
    provider: "datanodes",
    user_action: "turnstile",
  });

  // One click arms the package; every sibling then raises its own challenge and
  // solves itself. Those must show in the row badge, never as a prompt.
  const prompts = collectPendingChallenges(undefined, [
    part("1", { challenge_id: "chal-1" }),
    part("2", { challenge_id: "chal-2", solver_active: true }),
    part("3", { challenge_id: "chal-3", solver_active: true }),
    part("4", { challenge_id: "chal-4", solver_active: true }),
  ]);
  assert.equal(prompts.length, 1, "auto-solving siblings raised extra CAPTCHA prompts");
  assert.equal(prompts[0].id, "chal-1");

  // Once the armed solve finishes and a part genuinely needs a human again, it
  // must come back -- suppression is about solver_active, not about being a sibling.
  const returning = collectPendingChallenges(undefined, [
    part("2", { challenge_id: "chal-2", solver_active: false }),
  ]);
  assert.equal(returning.length, 1, "a challenge that stopped auto-solving was hidden from the user");

  // The engine-supplied pending list is filtered on the same rule.
  const enginePending = collectPendingChallenges(
    [{ id: "chal-9", provider_id: "datanodes", captcha_type: "turnstile",
       params: { coordinator_state: "clearcote_active" }, timeout_seconds: 90,
       created_at: 0, expires_at: 0, time_remaining: 90, status: "pending" } as any],
    []);
  assert.equal(enginePending.length, 0, "an actively-solving engine challenge was surfaced as a prompt");
  console.log("  [PASS] Auto-Solve Banner Suppression");
}

console.log("All Multipart UI State Tests Passed Successfully!");
