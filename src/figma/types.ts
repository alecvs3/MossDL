import type {
  AppPage,
  SettingsTab as EngineSettingsTab,
  RouteProfile,
  LinkGrabberEntry,
  ProviderMetadata,
  CaptchaChallenge,
  CaptureBatch,
  CaptureCandidate,
  CaptureImportResult,
  FileCategory,
  CollectionPlan,
  CollectionEnqueueResult,
  MediaPlan,
  Task,
  TaskState,
} from "../api";

export type DownloadStatus = "downloading" | "paused" | "queued" | "error" | "completed" | "cancelled" | "needs_user";
export type HistoryStatus = "completed" | "cancelled" | "failed";
export type Page = AppPage;
export type SettingsTab = EngineSettingsTab;

export type Provider =
  | "gdrive"
  | "transferit"
  | "mediafire"
  | "pixeldrain"
  | "1fichier"
  | "krakenfiles"
  | "cyberdrop"
  | "mega"
  | "direct"
  | (string & {});

export interface Download {
  id: string;
  name: string;
  url: string;
  provider?: string;
  destination?: string;
  size: number;
  downloaded: number;
  speed: number;
  eta: string;
  status: DownloadStatus;
  type: string;
  /** The engine's file category (engine/file_classifier.py). */
  category?: string | null;
  added: string;
  rawState?: TaskState;
  detailedStatus?: string;
  stage?: string;
  stageDetail?: Record<string, unknown>;
  stageEnteredAt?: number;
  error?: string;
  pausedReason?: string;
  user_action?: string;
  user_challenge?: Record<string, unknown>;
}

export interface DownloadWithHistory extends Download {
  speedHistory: number[];
  selected: boolean;
}

export interface HistoryItem {
  id: string;
  name: string;
  size: number;
  status: HistoryStatus;
  date: string;
  duration: string;
  type: string;
  /** The engine's file category (engine/file_classifier.py). */
  category?: string | null;
  savedTo: string;
  url: string;
  provider?: string;
  /** Epoch seconds the download finished (or last changed). */
  finishedAt?: number | null;
  /** Wall time from start to finish, when both are known. */
  durationSeconds?: number | null;
  error?: string | null;
}

export interface Candidate {
  id: string;
  name: string;
  size: number;
  type: string;
  selected: boolean;
}

export interface ModalData {
  url: string;
  provider: Provider;
  resolvedName: string;
  size: number | null;
  mime: string;
  redirects: number;
  confidence: number;
  candidates: Candidate[];
  savePath: string;
  queue: string;
  selectedItemIds?: string[];
  duplicateStrategy?: "skip" | "overwrite" | "rename" | "prompt";
  createSubfolder?: boolean;
  subfolderName?: string;
  autoExtract?: boolean;
}

export interface AppSettings {
  autoStart: boolean;
  systemTray: boolean;
  startWithWindows: boolean;
  clearOnExit: boolean;
  confirmClear?: boolean;
  /** JSON: command id → bindings, for shortcuts the user changed. */
  hotkeys?: string;
  clipboardWatcher: boolean;
  maxConcurrent: string;
  speedLimit: boolean;
  speedLimitVal: string;
  proxy: boolean;
  proxyAddr: string;
  retryFailed: boolean;
  retryCount: string;
  acrylic: boolean;
  rowSize: "small" | "medium" | "large";
  showSpeeds: boolean;
  theme?: "dark" | "light" | "system";
  colorAccent: string;
  notifComplete: boolean;
  notifFailed: boolean;
  notifPause: boolean;
  notifSound: boolean;
  savePath: string;
  captchaAcknowledged: boolean;
  captchaMaster: boolean;
  captcha2captcha: boolean;
  captcha2captchaKey: string;
  captchaFlare: boolean;
  captchaFlareEndpoint: string;
  captchaAutoOpenManual?: boolean;
  captchaShowAutoBanner?: boolean;
  captchaAutoSolve?: boolean;
  captchaAudio: boolean;
  captchaHcaptcha: boolean;
  captchaRecaptcha: boolean;
  captchaPositional: boolean;
  captchaAutoSkip: boolean;
  captchaAutoSkipSecs: string;
  captchaSound: boolean;
  routeType: string;
  activeLocation: string;
  autoSwitchOnQuota: boolean;
  switchIncludesProxies: boolean;
  allowDirectFallback: boolean;
  connectionsPerFile: string;
  timeoutSeconds: string;
  /** Block ads and trackers (crawls, capture review, the browser). */
  adblock: boolean;
  /** Fetch uBlock Origin's full lists (~4 MB, weekly) instead of the built-in list. */
  adblockFullLists: boolean;
  colorMode: string;
  language: string;
  soundPreset: string;
  themeName?: string;
  uiScale?: string;
  autoFullscreenScale?: boolean;
  fontFamily?: string;
}

export type MenuId = "file" | "view" | "tools" | "help" | "dev";

export type FigmaAppProps = {
  page: Page;
  onPageChange: (page: Page) => void;
  downloads: DownloadWithHistory[];
  history?: HistoryItem[];
  settings: AppSettings;
  routes?: RouteProfile[];
  linkEntries?: LinkGrabberEntry[];
  defaultSavePath: string;
  onAddUrl?: (
    url: string,
    destination?: string,
    selectedItemIds?: string[],
    duplicateStrategy?: "skip" | "overwrite" | "rename" | "prompt",
    displayName?: string,
    autoExtract?: boolean,
    shouldRefresh?: boolean
  ) => void | Promise<void>;
  onTaskAction?: (task: DownloadWithHistory, action: "pause" | "resume" | "stop" | "restart") => void;
  onBulkAction?: (action: "pause" | "resume" | "stop" | "restart", ids: string[]) => void;
  onRemoveTask?: (id: string | string[], deleteFiles?: boolean) => void;
  onClearCompleted?: () => void;
  onSettingChange?: (tab: SettingsTab, key: string, value: string | boolean) => void;
  onLinkAdd?: (text: string) => Promise<void>;
  onLinkUpdate?: (id: string, selected: boolean) => Promise<void>;
  onLinkBulkUpdate?: (ids: string[], selected: boolean) => Promise<void>;
  onLinkDelete?: (id: string) => Promise<void>;
  onLinkBulkDelete?: (ids: string[]) => Promise<void>;
  onLinkInspect?: (id: string, resolve: boolean) => Promise<Record<string, unknown>>;
  onLinkRetry?: (ids: string[]) => Promise<void>;
  onLinkEnqueue?: (ids: string[]) => Promise<void>;
  onLinkClear?: () => Promise<void>;
  onHistoryRetry?: (id: string) => void;
  onHistoryClear?: () => void;
  onPickDirectory?: () => Promise<string | null>;
  onSaveSecret?: (name: string, value: string) => Promise<void>;
  onPrioritizeTask?: (id: string) => void;
  onOpenFolder?: (path: string) => void;
  providers?: ProviderMetadata[];
  captchaPending?: CaptchaChallenge[];
  autoExtractMultipartDefault?: boolean;
  onCaptchaSolve?: (id: string, solution: Record<string, unknown>, generation?: number) => Promise<void>;
  onCaptchaSkip?: (id: string, generation?: number) => Promise<void>;
  captureBatches?: CaptureBatch[];
  onDismissCaptureBatch?: (batchId: string) => void;
  onCaptureImport?: (params: {
    batchId: string;
    candidateId?: string;
    candidateIndex: number;
    destination: string;
    category?: FileCategory;
    queueId: string;
    addAnyway?: boolean;
  }) => Promise<CaptureImportResult>;
  onMediaImport?: (candidate: CaptureCandidate, url: string) => Promise<CaptureImportResult | Task>;
  onMediaPlan?: (url: string) => Promise<MediaPlan>;
  collectionPlans?: CollectionPlan[];
  activeCollection?: CollectionPlan | null;
  onCollectionOpen?: (id: string) => Promise<CollectionPlan>;
  onCollectionSelect?: (id: string, itemIds: string[], selected?: boolean) => Promise<CollectionPlan>;
  onCollectionEnqueue?: (id: string, itemIds: string[], destination: string) => Promise<CollectionEnqueueResult>;
  onCollectionCancel?: (id: string) => Promise<CollectionPlan>;
  onCollectionContinue?: (id: string) => Promise<CollectionPlan>;
  onOpenSetupWizard?: () => void;
};

export { formatBytes, formatSpeed, formatPercent, formatDuration } from "../lib/format.ts";
export const fmtBytes = (mb: number) => (mb >= 1000 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(2)} MB`);
export const pct = (d: Download) => {
  if (d.status === "completed") return 100;
  const staged = stageProgressBytes(d);
  if (staged) {
    return Math.min(99, Math.max(0, Math.round((staged.progress / staged.expected) * 100)));
  }
  return d.size > 0
    ? Math.min(99, Math.max(0, Math.round((Math.min(d.downloaded, d.size) / d.size) * 100)))
    : 0;
};

/** Engine lifecycle stages that represent work in progress (never terminal). */
export const ACTIVE_STAGES = [
  "resolving_metadata",
  "hoster_wait_timer",
  "captcha_challenge_detected",
  "captcha_solving",
  "captcha_verifying",
  "direct_link_acquired",
  "downloading",
  "verifying_integrity",
  "unraring_pending",
  "unraring_extracting",
  "archive_cleanup",
] as const;

/** Archive pipeline stages (post-download continuation of a multipart package). */
export const ARCHIVE_STAGES = ["unraring_pending", "unraring_extracting", "archive_cleanup"] as const;

/** Stages whose `stage_detail` carries byte progress for the carrier task. */
const PROGRESS_STAGES = ["verifying_integrity", "unraring_pending", "unraring_extracting", "archive_cleanup"] as const;

export type StatusBucket = "transferring" | "waiting" | "completed" | "failed";

export function isArchiveStage(stage?: string | null): boolean {
  return !!stage && (ARCHIVE_STAGES as readonly string[]).includes(stage);
}

/**
 * True when the engine's archive job is what drives this task's current stage.
 *
 * `verifying_integrity` is shared: it is both the post-download hash check and
 * the archive job's own verify step (`ArchiveVerifying` maps to it in
 * engine/lifecycle.py). The engine stamps `source: "archive"` on every detail it
 * emits for an archive job, so that is the only reliable discriminator. A stage
 * check alone reports "not archiving" mid-extraction and lets the package render
 * as complete while the archive is still running.
 */
export function isArchiveDrivenStage(d: Pick<Download, "stage" | "stageDetail">): boolean {
  if (isArchiveStage(d.stage)) return true;
  const detail = (d.stageDetail || {}) as Record<string, unknown>;
  return d.stage === "verifying_integrity" && detail.source === "archive";
}

export function isActiveStage(stage?: string | null): boolean {
  return !!stage && (ACTIVE_STAGES as readonly string[]).includes(stage);
}

/**
 * True only when the engine reports the task in the transport stage.
 * A package leader in `postprocessing` is *not* genuinely downloading, so
 * speed/ETA must stay blank rather than display a fabricated 0.00 MB/s.
 */
export function isGenuinelyDownloading(d: Pick<Download, "status" | "rawState">): boolean {
  return d.status === "downloading" && (d.rawState === undefined || d.rawState === "downloading");
}

/**
 * Real byte progress reported by the engine on verify/archive stages.
 * `stage_detail.expected_size` / `progress_bytes` / `observed_size` are BYTES
 * (engine truth); callers convert to the view-model MB unit when aggregating.
 */
export function stageProgressBytes(d: Pick<Download, "stage" | "stageDetail">): { expected: number; progress: number } | null {
  if (!d.stage || !(PROGRESS_STAGES as readonly string[]).includes(d.stage)) return null;
  const detail = (d.stageDetail || {}) as Record<string, unknown>;
  // An extract job's `expected_size` is the sum of the INPUT volumes while its
  // `progress_bytes`/`observed_size` count EXTRACTED OUTPUT bytes (engine
  // service.py builds them from the part manifest and the worker result
  // respectively). Dividing one by the other is meaningless: it reads 0% for a
  // package that is fully downloaded. The engine reports no incremental output
  // progress for extraction, so there is no honest ratio to show -- callers fall
  // back to the download total and communicate extraction via the stage label.
  if (detail.operation === "extract") return null;
  const expected = typeof detail.expected_size === "number" ? detail.expected_size : null;
  const progress =
    typeof detail.progress_bytes === "number"
      ? detail.progress_bytes
      : typeof detail.observed_size === "number"
        ? detail.observed_size
        : null;
  if (expected === null || expected <= 0 || progress === null) return null;
  return { expected, progress: Math.min(Math.max(0, progress), expected) };
}

export function downloadBucket(d: Download): StatusBucket {
  if (d.status === "needs_user" || d.rawState === "needs_user") return "waiting";
  if (d.status === "downloading" || isActiveStage(d.stage)) return "transferring";
  if (d.status === "completed") return "completed";
  if (d.status === "error" || d.status === "cancelled") return "failed";
  return "waiting";
}

/**
 * Aggregate package status from the per-part view models. Archive stages are
 * in-progress work even when every part's scheduler state is `completed`, so
 * the package must stay in Transferring while unraring/cleaning up.
 */
export function aggregatePackageStatus(tasks: Download[]): DownloadStatus {
  if (tasks.length === 0) return "queued";
  if (tasks.some((t) => t.status === "needs_user" || t.rawState === "needs_user")) return "needs_user";
  const active = tasks.some(
    (t) =>
      t.status === "downloading" ||
      isActiveStage(t.stage) ||
      t.rawState === "resolving" ||
      t.rawState === "preflight" ||
      t.rawState === "verifying" ||
      t.rawState === "postprocessing" ||
      t.rawState === "retrying"
  );
  if (active) return "downloading";
  if (tasks.every((t) => t.status === "completed")) return "completed";
  if (tasks.some((t) => t.status === "paused")) return "paused";
  if (tasks.some((t) => t.status === "error" || t.status === "cancelled")) return "error";
  return "queued";
}

/**
 * The single part whose lifecycle stage represents the package on a collapsed
 * header row.
 *
 * A running hoster countdown outranks an active transfer: it is transient,
 * time-critical, and shown nowhere else when the package is collapsed, whereas
 * "Downloading" is already conveyed by the progress bar and the speed column.
 * Preferring the transfer meant that as soon as part 1 began downloading, every
 * sibling's countdown became invisible unless the package happened to be
 * expanded.
 */
export function packageStageCarrier(tasks: Download[]): Download | undefined {
  return (
    tasks.find((task) => task.stage === "hoster_wait_timer") ||
    tasks.find((task) => isGenuinelyDownloading(task)) ||
    tasks.find((task) => task.rawState === "resolving" || task.rawState === "preflight") ||
    tasks.find((task) => task.rawState === "needs_user") ||
    tasks.find((task) => isArchiveDrivenStage(task) || task.rawState === "postprocessing")
  );
}

export function packageBucket(tasks: Download[]): StatusBucket {
  const status = aggregatePackageStatus(tasks);
  if (status === "downloading") return "transferring";
  if (status === "completed") return "completed";
  if (status === "error") return "failed";
  return "waiting";
}

export interface PackageProgress {
  /** 0-100 aggregate percentage. */
  percent: number;
  /** Total package size in MEGABYTES (view-model unit; matches Download.size and fmtBytes). */
  totalMB: number;
  /** Completed bytes expressed in MEGABYTES. */
  completedMB: number;
  /** Remaining bytes expressed in MEGABYTES. */
  remainingMB: number;
  /** Aggregate speed in MB/s across genuinely downloading parts. */
  speedMBps: number;
  /** True when a part carries real engine archive/verify byte progress. */
  stageCarrier: boolean;
}

const BYTES_PER_MB = 1024 * 1024;

/**
 * Package-level aggregate progress. View-model sizes are MEGABYTES (see
 * liveAdapters.mb) while `stage_detail` progress is BYTES; this helper does
 * the conversion so call sites never mix the units. Tasks on a verify/archive
 * stage contribute their real engine byte progress (weighting the archive
 * carrier); all other tasks contribute their downloaded bytes.
 */
export function packageProgress(tasks: Download[]): PackageProgress {
  let totalBytes = 0;
  let completedBytes = 0;
  let speedMBps = 0;
  let stageCarrier = false;
  // An archive job's progress covers the whole package (expected_size is the
  // sum of every volume), so it drives the aggregate bar while it runs.
  const archiveCarrier = tasks.find((task) => isArchiveDrivenStage(task) && stageProgressBytes(task) !== null);
  if (archiveCarrier) {
    const staged = stageProgressBytes(archiveCarrier)!;
    for (const task of tasks) {
      totalBytes += Math.max(0, task.size || 0) * BYTES_PER_MB;
      if (isGenuinelyDownloading(task)) speedMBps += task.speed || 0;
    }
    completedBytes = staged.progress;
    return {
      percent: Math.min(100, Math.max(0, (staged.progress / staged.expected) * 100)),
      totalMB: totalBytes / BYTES_PER_MB,
      completedMB: completedBytes / BYTES_PER_MB,
      remainingMB: Math.max(0, (staged.expected - staged.progress) / BYTES_PER_MB),
      speedMBps,
      stageCarrier: true,
    };
  }
  for (const task of tasks) {
    const sizeBytes = Math.max(0, task.size || 0) * BYTES_PER_MB;
    totalBytes += sizeBytes;
    const staged = stageProgressBytes(task);
    if (staged) {
      stageCarrier = true;
      completedBytes += staged.progress;
    } else if (sizeBytes > 0) {
      completedBytes += Math.min(sizeBytes, Math.max(0, task.downloaded || 0) * BYTES_PER_MB);
    }
    if (isGenuinelyDownloading(task)) speedMBps += task.speed || 0;
  }
  const percent = totalBytes > 0 ? Math.min(100, Math.max(0, (completedBytes / totalBytes) * 100)) : 0;
  return {
    percent,
    totalMB: totalBytes / BYTES_PER_MB,
    completedMB: completedBytes / BYTES_PER_MB,
    remainingMB: Math.max(0, (totalBytes - completedBytes) / BYTES_PER_MB),
    speedMBps,
    stageCarrier,
  };
}

/**
 * Live `hoster_wait_timer` countdown. `stage_detail.countdown_seconds` is the
 * engine-authored "seconds remaining" anchor and `countdown_captured_at` is the
 * engine wall-clock epoch seconds when that anchor was written; the UI projects
 * the remaining time between snapshots. Older payloads without the capture
 * timestamp fall back to the stage entry time.
 */
export function stageCountdownRemaining(
  d: Pick<Download, "stage" | "stageDetail" | "stageEnteredAt">,
  nowSeconds: number = Date.now() / 1000
): number | null {
  if (d.stage !== "hoster_wait_timer") return null;
  const detail = (d.stageDetail || {}) as Record<string, unknown>;
  const reported = typeof detail.countdown_seconds === "number" ? detail.countdown_seconds : null;
  const capturedAt = typeof detail.countdown_captured_at === "number" ? detail.countdown_captured_at : null;
  const enteredAt = typeof d.stageEnteredAt === "number" ? d.stageEnteredAt : null;
  if (reported === null && enteredAt === null) return null;
  const anchor = capturedAt ?? enteredAt;
  if (anchor !== null) {
    const elapsed = Math.max(0, nowSeconds - anchor);
    return Math.max(0, Math.round((reported ?? 0) - elapsed));
  }
  return Math.max(0, Math.round(reported ?? 0));
}

/** Engine-authored quota wording (service.py quota branch); never inferred from generic errors. */
const ENGINE_QUOTA_PREFIX = "download quota reached";

/**
 * Returns the real engine quota signal (or null). Only structured stage_detail
 * markers, an engine quota paused_reason, or the engine's canonical quota
 * message qualify — a plain task failure never does.
 */
export function quotaSignal(d: Download): string | null {
  const detail = (d.stageDetail || {}) as Record<string, unknown>;
  for (const key of ["failure_class", "category", "outcome", "reason"]) {
    const value = detail[key];
    if (typeof value === "string" && value.toLowerCase().includes("quota")) return value;
  }
  const paused = (d.pausedReason || "").toLowerCase();
  if (paused.includes("quota")) return d.pausedReason || "quota";
  const error = (d.error || "").trim().toLowerCase();
  if (error.startsWith(ENGINE_QUOTA_PREFIX)) return d.error || null;
  return null;
}

/**
 * Real CAPTCHA challenges only: the engine pending list, plus needs_user tasks
 * whose engine-recorded challenge carries a real id. Never synthesizes an id
 * for a challenge the engine did not create, and dedupes shared package
 * challenges (sibling parts reference the same challenge id).
 */
export function collectPendingChallenges(
  captchaPending: CaptchaChallenge[] | undefined,
  tasks: Download[]
): CaptchaChallenge[] {
  const byId = new Map<string, CaptchaChallenge>();
  for (const challenge of captchaPending ?? []) {
    // Same rule as below: a challenge Clearcote is already working on is not a
    // prompt. The engine marks it on the challenge params when it arms a solve.
    if (challenge?.params?.coordinator_state === "clearcote_active") continue;
    if (challenge?.id) byId.set(challenge.id, challenge);
  }
  for (const task of tasks) {
    if (!(task.status === "needs_user" || task.rawState === "needs_user")) continue;
    const challenge = (task.user_challenge || {}) as Record<string, unknown>;
    // A challenge the engine is already solving needs nothing from the user.
    // Once one click arms a package, every sibling raises its own challenge and
    // auto-solves; surfacing those as prompts produced a burst of banners for
    // work nobody had to do. The row's own status badge still shows the solve in
    // progress, which is where that belongs.
    if (challenge.solver_active === true) continue;
    const id =
      typeof challenge.id === "string"
        ? challenge.id
        : typeof challenge.challenge_id === "string"
          ? challenge.challenge_id
          : null;
    if (!id || byId.has(id)) continue;
    byId.set(id, {
      id,
      task_id: task.id,
      provider_id: typeof challenge.provider_id === "string" ? challenge.provider_id : task.provider || "host",
      captcha_type:
        typeof challenge.captcha_type === "string" ? challenge.captcha_type : task.user_action || "",
      params: (challenge.params as Record<string, any>) || {},
      timeout_seconds: typeof challenge.timeout_seconds === "number" ? challenge.timeout_seconds : 0,
      created_at: typeof challenge.created_at === "number" ? challenge.created_at : 0,
      expires_at: typeof challenge.expires_at === "number" ? challenge.expires_at : 0,
      time_remaining: typeof challenge.time_remaining === "number" ? challenge.time_remaining : 0,
      status: typeof challenge.status === "string" ? challenge.status : "pending",
      loopback_url: typeof challenge.loopback_url === "string" ? challenge.loopback_url : null,
    });
  }
  return Array.from(byId.values());
}

export function captchaTypeLabel(type?: string | null): string {
  const value = (type || "").toLowerCase();
  if (value.includes("turnstile")) return "Cloudflare Turnstile";
  if (value.includes("recaptcha")) return "reCAPTCHA";
  if (value.includes("hcaptcha")) return "hCaptcha";
  if (value.includes("image")) return "Image verification";
  if (value.includes("positional")) return "Positional CAPTCHA";
  if (value.includes("browser")) return "Browser verification";
  return value ? value.replace(/_/g, " ") : "Verification";
}

export function captchaBannerCopy(type?: string | null): string {
  const value = (type || "").toLowerCase();
  if (value.includes("turnstile")) return "Host requires Cloudflare Turnstile verification to release download links.";
  if (value.includes("recaptcha")) return "Host requires reCAPTCHA verification to release download links.";
  if (value.includes("hcaptcha")) return "Host requires hCaptcha verification to release download links.";
  if (value.includes("image")) return "Host requires an image code to release the download link.";
  if (value.includes("browser")) return "Open the host page in your browser to complete verification.";
  return "Host requires human verification to release download links.";
}
