import type { EventEnvelope, Task } from "../api";

export type ArchiveChoice = {
  taskId: string;
  inputPath: string;
  outputDirectory: string;
  expectedSize: number;
  packageKey: string;
};

/** Durable engine event types consumed as idempotent deltas. */
const TASK_DELTA_KEYS = [
  "state",
  "error",
  "stage",
  "stage_detail",
  "stage_entered_at",
  "user_action",
  "user_challenge",
  "paused_reason",
  "completed_bytes",
  "size",
  "speed_bytes_per_second",
  "average_speed_bytes_per_second",
  "eta_seconds",
  "integrity_state",
  "revision",
] as const;

/**
 * `Archive*` events that are NOT reports from a running archive job.
 *
 * The generic `startsWith("Archive")` branch stamps `stage_detail.source =
 * "archive"` onto the task. These events describe something that needs the
 * user's attention instead, and stamping them would leave a finished task
 * carrying archive-flavoured detail for a job that never ran.
 */
const ARCHIVE_NON_STAGE_EVENTS = new Set([
  "ArchiveChoiceRequired",
  "ArchivePackageIncomplete",
]);

/** Archive events -> lifecycle stage, mirroring engine/lifecycle.py. */
const ARCHIVE_EVENT_STAGE: Record<string, string> = {
  ArchiveQueued: "unraring_pending",
  ArchiveStarted: "unraring_extracting",
  ArchiveVerifying: "verifying_integrity",
  ArchiveCleanupPending: "archive_cleanup",
  ArchiveCompleted: "archive_cleanup",
};

const STAGE_ORDER: Record<string, number> = {
  resolving_metadata: 1,
  hoster_wait_timer: 2,
  captcha_challenge_detected: 3,
  captcha_solving: 4,
  captcha_verifying: 5,
  direct_link_acquired: 6,
  downloading: 7,
  verifying_integrity: 8,
  unraring_pending: 9,
  unraring_extracting: 10,
  archive_cleanup: 11,
  completed: 12,
};

function stageRank(stage?: string | null): number {
  return (stage && STAGE_ORDER[stage]) || 0;
}

function applyTaskPayload(task: Task, payload: Record<string, unknown>): Task {
  if (
    typeof payload.revision === "number" &&
    typeof task.revision === "number" &&
    payload.revision < task.revision
  ) {
    return task;
  }
  let changed = false;
  const next: Task = { ...task };
  for (const key of TASK_DELTA_KEYS) {
    if (!(key in payload)) continue;
    (next as any)[key] = payload[key];
    changed = true;
  }
  return changed ? next : task;
}

function applyArchivePayload(
  task: Task,
  eventType: string,
  payload: Record<string, unknown>,
  enteredAt: number | undefined
): Task {
  const targetStage = ARCHIVE_EVENT_STAGE[eventType];
  const detail: Record<string, unknown> = {
    ...(task.stage_detail || {}),
    source: "archive",
    event: eventType,
  };
  for (const key of ["operation", "state", "progress_bytes", "expected_size", "observed_size", "error"]) {
    if (key in payload && payload[key] !== undefined && payload[key] !== null) detail[key] = payload[key];
  }
  const canAdvance = !!targetStage && stageRank(targetStage) >= Math.max(stageRank(task.stage), 7);
  const next: Task = { ...task, stage_detail: detail };
  if (canAdvance && task.stage !== targetStage) {
    next.stage = targetStage;
    next.stage_entered_at = enteredAt ?? Date.now() / 1000;
  }
  return next;
}

export function extractArchiveChoices(events: EventEnvelope[]): ArchiveChoice[] {
  const choices: ArchiveChoice[] = [];
  for (const event of events) {
    if (event.event_type !== "ArchiveChoiceRequired") continue;
    const payload = event.payload || {};
    const taskId =
      typeof payload.task_id === "string" ? payload.task_id : typeof event.task_id === "string" ? event.task_id : null;
    const inputPath = typeof payload.input_path === "string" ? payload.input_path : null;
    if (!taskId || !inputPath) continue;
    choices.push({
      taskId,
      inputPath,
      outputDirectory: typeof payload.output_directory === "string" ? payload.output_directory : "",
      expectedSize: typeof payload.expected_size === "number" ? payload.expected_size : 0,
      packageKey: typeof payload.package_key === "string" ? payload.package_key : taskId,
    });
  }
  return choices;
}

/** A package that can never be extracted because a volume is missing. */
export type IncompletePackage = {
  taskId: string;
  packageName: string;
  missingParts: number[];
  foundParts: number[];
  directory: string;
};

/**
 * Packages the engine refused to extract because their volume set has a hole.
 *
 * The engine keeps every downloaded volume and reports
 * `[ARCHIVE_PACKAGE_INCOMPLETE]`, but without surfacing it the user just sees
 * finished downloads and is never told the package is unusable.
 */
export function extractIncompletePackages(events: EventEnvelope[]): IncompletePackage[] {
  const out: IncompletePackage[] = [];
  for (const event of events) {
    if (event.event_type !== "ArchivePackageIncomplete") continue;
    const payload = event.payload || {};
    const taskId =
      typeof payload.task_id === "string" ? payload.task_id : typeof event.task_id === "string" ? event.task_id : null;
    const packageName = typeof payload.package === "string" ? payload.package : null;
    if (!taskId || !packageName) continue;
    const numbers = (value: unknown): number[] =>
      Array.isArray(value) ? value.filter((item): item is number => typeof item === "number") : [];
    out.push({
      taskId,
      packageName,
      missingParts: numbers(payload.missing_parts),
      foundParts: numbers(payload.found_parts),
      directory: typeof payload.directory === "string" ? payload.directory : "",
    });
  }
  return out;
}

/**
 * Idempotent engine-event application. The durable outbox is the low-latency
 * delta source; the UI snapshot remains the resync authority, so stale
 * payloads (revision behind the local task) are dropped rather than applied.
 */
export function applyEngineEvents(tasks: Task[], events: EventEnvelope[]): Task[] {
  if (events.length === 0) return tasks;
  let changed = false;
  const byId = new Map(tasks.map((task) => [task.id, task]));
  for (const event of events) {
    const payload = event.payload || {};
    const taskId = typeof event.task_id === "string" ? event.task_id : null;
    if (!taskId) continue;
    const task = byId.get(taskId);
    if (!task) continue;
    let next = task;
    if (event.event_type.startsWith("Archive") && !ARCHIVE_NON_STAGE_EVENTS.has(event.event_type)) {
      next = applyArchivePayload(
        task,
        event.event_type,
        payload,
        typeof event.created_at === "number" ? event.created_at : undefined
      );
    } else if (event.event_type === "TaskStageChanged" || event.event_type === "TaskUpdated" || "state" in payload) {
      next = applyTaskPayload(task, payload);
    }
    if (next !== task) {
      byId.set(taskId, next);
      changed = true;
    }
  }
  if (!changed) return tasks;
  return tasks.map((task) => byId.get(task.id) || task);
}
