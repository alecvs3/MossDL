import type { LinkGrabberEntry, Task, TaskState, UiSettings } from "../api";
import type { DownloadStatus, DownloadWithHistory } from "./FigmaApp";
import { getMultiPartFolder } from "../lib/multipart.ts";
import { stageProgressBytes } from "./types.ts";
import { bestTitle, extractUrlCandidates } from "../lib/titleScorer.ts";

const mb = (bytes?: number | null) => (bytes && bytes > 0 ? bytes / (1024 * 1024) : 0);

function extension(task: Task): string {
  const name = task.display_name || task.source_url;
  const value = name.split(/[?#]/, 1)[0].split("/").pop() || "FILE";
  return value.includes(".") ? value.split(".").pop()!.toUpperCase() : "FILE";
}

function displayTime(timestamp?: number | null): string {
  if (!timestamp) return "—";
  return new Date(timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function etaLabel(seconds?: number | null): string {
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  return total < 60 ? `${total}s` : `${Math.floor(total / 60)}m ${total % 60}s`;
}

const STAGE_LABELS: Record<string, string> = {
  resolving_metadata: "Getting metadata",
  hoster_wait_timer: "Waiting timer",
  captcha_challenge_detected: "CAPTCHA detected",
  captcha_solving: "Solving CAPTCHA",
  captcha_verifying: "Verifying CAPTCHA",
  direct_link_acquired: "Direct link ready",
  downloading: "Downloading",
  verifying_integrity: "Verifying",
  unraring_pending: "Waiting for archive parts",
  unraring_extracting: "Extracting",
  archive_cleanup: "Cleaning up archives",
  completed: "Completed",
  failed: "Failed",
};

function progressSuffix(stage: string | null | undefined, detail: Record<string, unknown> | undefined): string {
  if (!stage) return "";
  const staged = stageProgressBytes({ stage, stageDetail: detail });
  if (!staged) return "";
  return ` (${Math.round((staged.progress / staged.expected) * 100)}%)`;
}

/** Solver micro-steps, surfaced as detail on a stable `captcha_solving` stage. */
const SOLVER_STEP_LABELS: Record<string, string> = {
  navigating: "opening page",
  step_advance: "advancing to download",
  step2_clicked: "advancing to download",
  turnstile_detected: "widget found",
  turnstile_solved: "verifying token",
};

function stageDetailLabel(stage: string, detail: Record<string, unknown> | undefined): string | undefined {
  if (stage === "hoster_wait_timer") {
    const countdown = typeof detail?.countdown_seconds === "number" ? detail.countdown_seconds : undefined;
    return countdown !== undefined ? `Waiting timer (${countdown}s)...` : "Waiting timer...";
  }
  if (stage === "captcha_solving") {
    const solver = typeof detail?.solver === "string" ? detail.solver : undefined;
    // The solver's internal step refines the label WITHOUT the stage changing.
    // These used to each rewrite the task's top-level stage, so one CAPTCHA
    // read as solving -> getting metadata -> solving -> waiting timer.
    const step = typeof detail?.solver_step === "string" ? detail.solver_step : undefined;
    const stepLabel = step ? SOLVER_STEP_LABELS[step] : undefined;
    if (stepLabel) return `Solving CAPTCHA — ${stepLabel}`;
    return solver ? `Solving CAPTCHA (${solver})` : "Solving CAPTCHA";
  }
  if (stage === "captcha_challenge_detected" && detail?.manual_required) {
    return "CAPTCHA — action needed";
  }
  if (stage === "verifying_integrity") return `Verifying${progressSuffix(stage, detail)}`;
  if (stage === "unraring_extracting") return `Extracting${progressSuffix(stage, detail)}`;
  if (stage === "archive_cleanup") return "Cleaning up archives";
  return undefined;
}

function stageDetailedStatus(task: Task, state: TaskState): string | undefined {
  const stage = task.stage;
  const detail = (task.stage_detail || {}) as Record<string, unknown>;
  if (stage) {
    const specific = stageDetailLabel(stage, detail);
    if (specific) return specific;
    const label = STAGE_LABELS[stage];
    if (label) return label;
  }
  // Legacy fallback for engines predating the canonical stage machine.
  if (state === "pending_probe") return "Waiting for Part 1";
  if (state === "resolving") {
    const solverStage = task.user_challenge?.solver_stage as string | undefined;
    const countdownSec = typeof task.user_challenge?.countdown_seconds === "number" ? task.user_challenge.countdown_seconds as number : undefined;
    if (solverStage === "turnstile_detected") return "Solving Turnstile";
    if (solverStage === "turnstile_solved") return "Turnstile Solved";
    if (countdownSec !== undefined) return `Waiting countdown (${countdownSec}s)...`;
    if (solverStage === "countdown") return "Waiting countdown...";
    if (solverStage === "step2_clicked" || solverStage === "step_advance") return "Advancing step...";
    if (solverStage === "primed" || task.user_challenge?.primed) return "Primed (Ready)";
    return "Getting metadata";
  }
  return undefined;
}

/**
 * Explicit scheduler-state projection. `postprocessing` is active archive
 * work (not a waiting position), and `verifying` continues the transfer, so
 * both project to "downloading"; stage-aware renderers keep speed/ETA hidden
 * unless the task is genuinely in `downloading`.
 */
const STATE_STATUS: Record<TaskState, DownloadStatus> = {
  queued: "queued",
  pending_probe: "queued",
  resolving: "queued",
  preflight: "queued",
  needs_user: "needs_user",
  downloading: "downloading",
  verifying: "downloading",
  postprocessing: "downloading",
  paused: "paused",
  retrying: "queued",
  completed: "completed",
  failed: "error",
  canceled: "cancelled",
};

export function taskToFigmaDownload(task: Task, speedHistory: number[] = []): DownloadWithHistory {
  const state = task.state;
  const status = STATE_STATUS[state] ?? "queued";

  const stageDerived = stageDetailedStatus(task, state);
  let detailedStatus: string | undefined;
  if (stageDerived) detailedStatus = stageDerived;
  else if (state === "queued") detailedStatus = task.user_challenge?.primed ? "Primed (Ready)" : "Starting";
  else if (state === "downloading") detailedStatus = task.user_challenge?.waiting_storage_slot ? "Primed (Waiting for slot)" : "Downloading";
  else if (state === "preflight") detailedStatus = "Preparing";
  else if (state === "verifying") detailedStatus = "Verifying";
  else if (state === "postprocessing") detailedStatus = "Extracting";
  else if (state === "needs_user") {
    detailedStatus = task.user_challenge?.solver_error
      ? "CAPTCHA Solver Failed"
      : (task.user_action === "captcha" || task.user_action === "turnstile" || task.user_challenge) ? "CAPTCHA" : "Needs User";
  } else if (state === "retrying") detailedStatus = "Retrying";
  else if (state === "paused") detailedStatus = "Paused";
  else if (state === "completed") detailedStatus = "Completed";
  else if (state === "failed") detailedStatus = "Failed";
  else if (state === "canceled") detailedStatus = "Cancelled";

  return {
    id: task.id,
    name: bestTitle(
      [
        ...(task.display_name ? [{ text: task.display_name, source: "provider" as const }] : []),
        ...extractUrlCandidates(task.source_url),
      ],
      task.display_name || task.source_url
    ),
    url: task.source_url,
    provider: task.provider,
    // The backend records the resolved item's actual containing directory;
    // use it for UI actions while retaining the task root as a fallback.
    destination: task.folder_path || task.destination,
    size: mb(task.size),
    downloaded: task.size ? Math.min(mb(task.completed_bytes), mb(task.size)) : mb(task.completed_bytes),
    speed: mb(task.speed_bytes_per_second),
    eta: etaLabel(task.eta_seconds),
    status,
    type: extension(task),
    category: task.category ?? null,
    added: displayTime(task.started_at || task.telemetry_updated_at),
    speedHistory: speedHistory.map(mb),
    selected: false,
    rawState: task.state,
    detailedStatus,
    stage: task.stage || undefined,
    stageDetail: task.stage_detail || undefined,
    stageEnteredAt: task.stage_entered_at ?? undefined,
    error: task.error,
    pausedReason: task.paused_reason,
    user_action: task.user_action,
    user_challenge: task.user_challenge,
  };
}

export function tasksToFigmaDownloads(tasks: Task[], history: Record<string, number[]> = {}): DownloadWithHistory[] {
  return tasks.map((task) => taskToFigmaDownload(task, history[task.id] || []));
}

export function tasksToFigmaHistory(tasks: Task[]) {
  return tasks
    .filter((task) => ["completed", "failed", "canceled"].includes(task.state))
    .map((task) => {
      const finishedAt = task.finished_at || task.telemetry_updated_at || null;
      const durationSeconds = finishedAt && task.started_at && finishedAt >= task.started_at
        ? finishedAt - task.started_at
        : null;
      return {
        id: task.id,
        name: task.display_name || task.source_url,
        size: mb(task.size),
        status: task.state === "completed" ? "completed" as const : task.state === "canceled" ? "cancelled" as const : "failed" as const,
        date: displayTime(finishedAt || task.started_at),
        duration: durationSeconds === null ? "—" : etaLabel(durationSeconds),
        finishedAt,
        durationSeconds,
        error: task.error || null,
        type: extension(task),
        category: task.category ?? null,
        savedTo: task.folder_path || task.destination || "—",
        url: task.source_url,
        provider: task.provider,
      };
    });
}

export function settingsToFigma(settings: UiSettings, destination: string): Record<string, unknown> {
  return {
    ...settings.general,
    ...settings.network,
    ...settings.appearance,
    ...settings.notifications,
    ...settings.captcha,
    ...settings.routes,
    savePath: destination,
  };
}

export function linkGrabberCount(entries: LinkGrabberEntry[]): number {
  return entries.filter((entry) => entry.state !== "ignored").length;
}

/**
 * Derives the effective destination path for a new download, automatically
 * nesting multi-part archives inside a dedicated package folder.
 */
export function resolveDestinationWithPackage(
  baseDestination: string,
  url: string,
  displayName?: string
): string {
  const target = displayName || url;
  const folder = getMultiPartFolder(target);
  if (!folder) return baseDestination;
  const cleanBase = baseDestination.replace(/[/\\]+$/, "");
  // Maintain platform-friendly path formatting
  const sep = cleanBase.includes("\\") ? "\\" : "/";
  // Idempotent: the modal may already have appended the package folder; never
  // nest it twice (…\<Pkg>\<Pkg>\).
  if (cleanBase.toLowerCase().endsWith(`${sep}${folder}`.toLowerCase())) return cleanBase;
  return `${cleanBase}${sep}${folder}`;
}

