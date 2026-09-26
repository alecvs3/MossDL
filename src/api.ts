import { invoke } from "@tauri-apps/api/core";
import type { ProviderTreeItem } from "./treePicker";

export type { ProviderTreeItem } from "./treePicker";

export type TaskState = "queued" | "pending_probe" | "resolving" | "preflight" | "needs_user" | "downloading" | "verifying" | "postprocessing" | "paused" | "retrying" | "completed" | "failed" | "canceled";
export type FileCategory = "video" | "audio" | "music" | "pictures" | "documents" | "archives" | "applications" | "code_data" | "subtitles" | "other" | string;
export type AppPage = "downloads" | "explore" | "collections" | "history" | "settings" | "captchas" | "routes";
export type SettingsTab = "general" | "network" | "appearance" | "notifications" | "captcha" | "routes" | "accounts" | "shortcuts";

export type Task = {
  id: string;
  source_url: string;
  destination: string;
  display_name?: string;
  provider?: string;
  state: TaskState;
  size?: number;
  completed_bytes: number;
  error?: string;
  retry_count?: number;
  route_profile_id?: string;
  user_action?: string;
  user_challenge?: Record<string, unknown>;
  paused_reason?: string;
  selected_item_ids?: string[] | null;
  account_ref?: string | null;
  revision?: number;
  recovery_reason?: string | null;
  integrity?: Record<string, unknown>;
  category?: FileCategory | null;
  folder_path?: string | null;
  package_key?: string | null;
  package_part_number?: number | null;
  package_part_count?: number | null;
  package_leader_id?: string | null;
  speed_bytes_per_second?: number;
  average_speed_bytes_per_second?: number;
  eta_seconds?: number | null;
  backend?: string | null;
  attempt_count?: number;
  started_at?: number | null;
  finished_at?: number | null;
  telemetry_updated_at?: number | null;
  integrity_state?: string;
  stage?: string | null;
  stage_detail?: Record<string, unknown>;
  stage_entered_at?: number | null;
  stage_history?: Array<Record<string, unknown>>;
};

export type TransferTelemetry = {
  speed_bytes_per_second: number;
  average_speed_bytes_per_second: number;
  eta_seconds?: number | null;
  backend?: string | null;
  attempt_count: number;
  started_at?: number | null;
  telemetry_updated_at?: number | null;
  integrity_state: string;
};

export type QueueOption = { id: string; name?: string; paused?: boolean; enabled?: boolean };

export type NotificationSink = {
  id: string;
  kind: "webhook" | "desktop" | string;
  endpoint?: string;
  credential_ref?: string | null;
  enabled: boolean;
  event_types: string[];
};

export type BackendCapabilities = Record<string, Record<string, unknown>>;
export type BackendHealth = {
  ok: boolean;
  engine_paused?: boolean;
  active_tasks?: number;
  plugins?: unknown[];
  provider_health?: unknown[];
};

export type UiSettings = {
  general: Record<string, unknown>;
  network: Record<string, unknown>;
  appearance: Record<string, unknown>;
  notifications: Record<string, unknown>;
  captcha: Record<string, unknown>;
  routes: Record<string, unknown>;
  accounts: Record<string, unknown>;
};

export type SettingsUpdateResult = {
  settings: UiSettings;
  changed: string[];
  revision: number;
};

export type UpdateCheck = { configured: boolean; available?: boolean; version?: string; current?: string; notes?: string | null };

/** Asks the release feed for a newer version (only builds released with an update key can). */
export async function checkForUpdate(): Promise<UpdateCheck> {
  return invoke<UpdateCheck>("update_check");
}

/** Downloads, verifies and installs the update; on Windows the installer takes over. */
export async function installUpdate(): Promise<void> {
  await invoke("update_install");
}

export async function windowAction(action: "minimize" | "toggle_maximize" | "close" | "destroy" | "start_dragging"): Promise<void> {
  try {
    await invoke("window_action", { action });
  } catch {
    // Non-Tauri browser / Storybook environment
  }
}

/** Ask Windows to paint Mica/Acrylic behind the window. Returns what applied ("none" if nothing did). */
export async function setWindowMaterial(material: "mica" | "acrylic" | "none"): Promise<string> {
  try {
    return await invoke<string>("set_window_material", { material });
  } catch {
    return "none";
  }
}

export type UiSnapshot = {
  revision: number;
  tasks: Task[];
  linkgrabber: LinkGrabberEntry[];
  queues: QueueOption[];
  accounts: ProviderAccount[];
  routes: RouteProfile[];
  providers: ProviderMetadata[];
  captcha_pending: CaptchaChallenge[];
  bandwidth: BandwidthProfile[];
  notifications: NotificationSink[];
  capabilities: BackendCapabilities;
  health: BackendHealth;
  settings: UiSettings;
};

export type CaptureCandidate = {
  candidate_id?: string;
  url: string;
  method?: "GET" | "HEAD" | string;
  headers?: Record<string, string>;
  referrer?: string | null;
  mime?: string | null;
  filename?: string | null;
  size?: number | null;
  confidence?: number;
  source?: string;
  request_id?: string | null;
  canonical_source?: string | null;
  source_fingerprint?: string | null;
  page_url?: string | null;
  page_origin?: string | null;
  session_ref?: string | null;
  /** Engine file category, added when batches are listed. */
  file_kind?: FileCategory;
  /** Set when the request is clearly not a download (bot challenge, analytics, ads). */
  noise?: string | null;
};

export type CaptureBatch = {
  batch_id: string;
  request_id: string;
  protocol_version: string;
  extension_origin: string;
  page_origin: string;
  page_url?: string | null;
  session_ref?: string | null;
  candidates: CaptureCandidate[];
  state: "pending" | "duplicate" | "imported" | string;
  acknowledged: boolean;
  imported_task_ids: string[];
  created_at: number;
  updated_at: number;
};

export type CaptureImportResult = {
  status: "imported" | "duplicate" | string;
  task?: Task;
  existing_task_id?: string | null;
  batch_id: string;
  candidate_id?: string | null;
  add_anyway_available?: boolean;
  add_anyway?: boolean;
};

export type CollectionNode = {
  node_id: string;
  parent_id?: string | null;
  depth: number;
  source_url: string;
  display_name: string;
  folder_path?: string;
  package_path?: string;
  status?: string;
  outcome?: string | null;
  size?: number | null;
  metadata?: Record<string, unknown>;
};

export type CollectionItem = CollectionNode & {
  stable_id: string;
  selected: boolean;
  error?: string | null;
};

export type CollectionPlan = {
  id: string;
  source_url: string;
  provider_id?: string | null;
  state: string;
  outcome?: string | null;
  graph_revision: number;
  has_more?: boolean;
  nodes: CollectionNode[];
  items: CollectionItem[];
  events: Array<Record<string, unknown>>;
};

export type CollectionEnqueueResult = {
  collection_id: string;
  queued: Task[];
  outcome?: string;
};

export type MediaPlan = {
  kind: "hls" | "dash" | string;
  manifest_url: string;
  url?: string;
  segments: Array<Record<string, unknown>>;
  variants: Array<Record<string, unknown>>;
  selected_variant?: Record<string, unknown> | null;
  encrypted: boolean;
  output_format?: string | null;
  total_duration?: number | null;
};

export type ProviderMetadata = {
  id: string;
  display_name: string;
  icon?: string | null;
  hosts: string[];
  enabled: boolean;
  capabilities?: string[];
  roles?: string[];
  category?: string | null;
};

export type TaskActionResult = {
  task?: Task;
  affected_ids: string[];
  skipped: Array<{ id: string; reason: string }>;
  revision?: number;
};

export type TaskConflict = {
  code: "revision_conflict" | string;
  message: string;
  current?: Task;
};

export type BulkTransferActionResult = {
  action: "pause" | "resume" | "stop";
  affected_ids: string[];
  skipped: Array<{ id: string; reason: string }>;
  affected_count: number;
  skipped_count: number;
};

export type TransferTreeItem = ProviderTreeItem;

export type RouteProfile = {
  id: string;
  kind: "direct" | "http_proxy" | "socks5" | "system_vpn" | "docker_socks5" | "wireguard" | string;
  endpoint?: string;
  region?: string;
  enabled?: boolean;
  credential_ref?: string | null;
  healthcheck_url?: string | null;
  /** WireGuard only: "core" runs the tunnel inside the app, "system" leaves it to the WireGuard app. */
  tunnel?: "core" | "system" | null;
  /** Account-based locations: the server's public key (the account's device key is in the secret store). */
  peer_public_key?: string | null;
};

export type LinkGrabberEntry = {
  id: string;
  url: string;
  normalized_url: string;
  source: string;
  source_context?: string | null;
  state: "pending" | "selected" | "ignored" | "resolved" | "failed" | "queued" | string;
  provider_id?: string | null;
  title?: string | null;
  error?: string | null;
  selected: boolean;
  created_at: number;
  updated_at: number;
  /** Engine file category for the link's target. */
  file_kind?: FileCategory;
};

export type ProviderAccount = {
  id: string;
  provider_id: string;
  label: string;
  credential_ref: string;
  account_type?: string;
  enabled: boolean;
  state: string;
  expires_at?: number | null;
  quota_bytes?: number | null;
  used_bytes: number;
  last_error?: string | null;
  health?: Record<string, unknown>;
};

export type BandwidthProfile = {
  id: string;
  name: string;
  scope: "global" | "queue" | "provider" | "account" | "task" | string;
  scope_key?: string | null;
  rate_bytes_per_second: number;
  windows: Array<{ start_minute: number; end_minute: number }>;
  enabled: boolean;
};

export type AdaptiveTransferStatus = {
  global_in_flight: number;
  controllers: Array<{
    key: string;
    concurrency: number;
    ceiling: number;
    in_flight: number;
    failures: number;
    cooldown_remaining: number;
    ewma_latency: number;
  }>;
};

let reqCounter = 0;

export async function devLog(
  level: string,
  tag: string,
  message: string,
  details?: Record<string, unknown>,
  error?: unknown
): Promise<void> {
  return emitLog(level, tag, message, details, error);
}

// --- Failure tracking for diagnostics ---
type EngineFailureRecord = {
  reqId: number;
  method: string;
  error: string;
  category: string;
  elapsed: number;
  timestamp: string;
};
const _engineFailureLog: EngineFailureRecord[] = [];
const MAX_FAILURE_LOG = 50;

function classifyEngineError(errStr: string): string {
  const lower = errStr.toLowerCase();
  if (lower.includes("pipe") || lower.includes("os error 232") || lower.includes("broken")) return "PIPE_BROKEN";
  if (lower.includes("exited unexpectedly") || lower.includes("process vanished")) return "ENGINE_CRASH";
  if (lower.includes("lock poisoned")) return "LOCK_POISONED";
  if (lower.includes("invalid json") || lower.includes("parse")) return "MALFORMED_RESPONSE";
  if (lower.includes("quota")) return "QUOTA_EXCEEDED";
  if (lower.includes("timeout") || lower.includes("timed out")) return "TIMEOUT";
  if (lower.includes("spawn") || lower.includes("could not start")) return "SPAWN_FAILURE";
  if (lower.includes("unavailable")) return "ENGINE_UNAVAILABLE";
  return "UNKNOWN";
}

/** Retrieve recent engine failures for external diagnostics. */
export function getEngineFailureLog(): readonly EngineFailureRecord[] {
  return _engineFailureLog;
}

export async function engine<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  const reqId = ++reqCounter;
  const start = performance.now();
  console.log(`%c[Engine Request #${reqId}] ${method}`, "color: #4da6f5; font-weight: bold;", params);
  try {
    const result = await invoke<T>("engine_request", { method, params });
    const elapsed = (performance.now() - start).toFixed(1);
    console.log(`%c✓ [Engine #${reqId}] ${method} (${elapsed}ms)`, "color: #34d399;", result);
    return result;
  } catch (err: unknown) {
    const elapsed = (performance.now() - start).toFixed(1);
    const errorStr = typeof err === "string"
      ? err
      : (err && typeof err === "object" && "message" in err
          ? String((err as Record<string, unknown>).message)
          : (err instanceof Error ? err.message : JSON.stringify(err)));

    const category = classifyEngineError(errorStr);
    const timestamp = new Date().toISOString();
    const record: EngineFailureRecord = {
      reqId,
      method,
      error: errorStr,
      category,
      elapsed: Number(elapsed),
      timestamp,
    };
    _engineFailureLog.push(record);
    if (_engineFailureLog.length > MAX_FAILURE_LOG) _engineFailureLog.shift();

    // Count recent failures of the same category (last 60s)
    const cutoff = Date.now() - 60_000;
    const recentSame = _engineFailureLog.filter(
      (r) => r.category === category && new Date(r.timestamp).getTime() > cutoff
    );

    console.error(
      `%c❌ [Engine Failure #${reqId}] ${method} (${elapsed}ms)\n` +
      `  Category:  ${category}\n` +
      `  Error:     ${errorStr}\n` +
      `  Timestamp: ${timestamp}\n` +
      `  Recent ${category} failures (60s): ${recentSame.length}`,
      "color: #f87171; font-weight: bold;",
    );
    console.error("  Params:", params);

    // Log stack trace for non-string errors
    if (err instanceof Error && err.stack) {
      console.error("  Stack:", err.stack);
    }

    // Warn if we're seeing repeated failures
    if (recentSame.length >= 3) {
      console.warn(
        `%c⚠️ [Engine] ${recentSame.length} ${category} failures in the last 60s — possible systemic issue`,
        "color: #fbbf24; font-weight: bold;",
      );
      console.warn("  Recent failure log:", recentSame);
    }

    void devLog("ERROR", `Engine::${method}`, errorStr, {
      method,
      params,
      elapsed_ms: Number(elapsed),
      category,
      recent_same_category: recentSame.length,
    });

    throw err;
  }
}

/** Hides captures from Explore; the engine keeps them as "dismissed". */
export async function dismissCaptureBatches(batchIds: string[]): Promise<{ dismissed: string[] }> {
  return engine<{ dismissed: string[] }>("capture_dismiss", { batch_ids: batchIds });
}

export async function listCaptureBatches(limit = 100): Promise<CaptureBatch[]> {
  const result = await engine<{ batches: CaptureBatch[] }>("capture_review_list", { limit });
  return result.batches ?? [];
}

export async function importCaptureCandidate(params: {
  batchId: string;
  candidateId?: string;
  candidateIndex?: number;
  destination?: string;
  category?: FileCategory;
  queueId?: string;
  duplicateStrategy?: "skip" | "rename" | string;
  addAnyway?: boolean;
}): Promise<CaptureImportResult> {
  return engine<CaptureImportResult>("capture_import", {
    batch_id: params.batchId,
    ...(params.candidateId ? { candidate_id: params.candidateId } : {}),
    ...(params.candidateIndex !== undefined ? { candidate_index: params.candidateIndex } : {}),
    ...(params.destination ? { destination: params.destination } : {}),
    ...(params.category ? { category: params.category } : {}),
    ...(params.queueId ? { queue_id: params.queueId } : {}),
    ...(params.duplicateStrategy ? { duplicate_strategy: params.duplicateStrategy } : {}),
    ...(params.addAnyway ? { add_anyway: true } : {}),
  });
}

export async function browserImportCandidate(params: {
  url: string;
  destination?: string;
  displayName?: string | null;
  headers?: Record<string, string>;
  referrer?: string | null;
  pageContext?: Record<string, unknown>;
  category?: FileCategory;
  queueId?: string;
}): Promise<Task> {
  return engine<Task>("browser_import", {
    url: params.url,
    destination: params.destination,
    display_name: params.displayName,
    headers: params.headers ?? {},
    referrer: params.referrer,
    page_context: params.pageContext ?? {},
    category: params.category,
    queue_id: params.queueId ?? "default",
  });
}

export async function planMedia(url: string): Promise<MediaPlan> {
  return engine<MediaPlan>("media_plan", { url });
}

export async function assembleMedia(params: { plan: MediaPlan; output: string; taskId?: string }): Promise<Record<string, unknown>> {
  return engine<Record<string, unknown>>("media_assemble", {
    plan: params.plan,
    output: params.output,
    ...(params.taskId ? { task_id: params.taskId } : {}),
  });
}

export async function listTasks(): Promise<Task[]> {
  return engine<Task[]>("list_tasks");
}

/** Rename a download's display label; renames the file on disk too if the task is completed. */
export async function renameTask(id: string, displayName: string, revision?: number): Promise<Task> {
  return engine<Task>("set_task_options", {
    id,
    display_name: displayName,
    ...(revision !== undefined ? { expected_revision: revision } : {}),
  });
}

export async function listCollectionPlans(): Promise<CollectionPlan[]> {
  return engine<CollectionPlan[]>("list_collection_plans");
}

export async function getCollection(id: string): Promise<CollectionPlan> {
  return engine<CollectionPlan>("collection_graph_get", { id });
}

export async function selectCollectionItems(id: string, itemIds: string[], selected = true): Promise<CollectionPlan> {
  return engine<CollectionPlan>("collection_select", { id, item_ids: itemIds, selected });
}

export async function enqueueCollection(id: string, itemIds: string[], destination: string): Promise<CollectionEnqueueResult> {
  return engine<CollectionEnqueueResult>("collection_enqueue", { id, item_ids: itemIds, destination });
}

export async function cancelCollection(id: string): Promise<CollectionPlan> {
  return engine<CollectionPlan>("collection_cancel", { id });
}

export async function continueCollection(id: string): Promise<CollectionPlan> {
  return engine<CollectionPlan>("collection_next_page", { id });
}

export type DestinationCollisionResult = {
  exists: boolean;
  primary_exists: boolean;
  path: string;
  size?: number | null;
  existing_items: string[];
  suggested_name?: string | null;
};

export async function checkDestinationCollision(
  destination: string,
  filename: string,
  items?: string[]
): Promise<DestinationCollisionResult> {
  return engine<DestinationCollisionResult>("check_destination_collision", {
    destination,
    filename,
    items: items ?? [],
  });
}

export async function addTask(
  url: string,
  destination: string,
  selectedItemIds?: string[],
  duplicateStrategy?: "skip" | "overwrite" | "rename" | "prompt",
  displayName?: string,
  autoExtract?: boolean
): Promise<Task> {
  return engine<Task>("add_task", {
    url,
    destination,
    ...(selectedItemIds ? { selected_item_ids: selectedItemIds } : {}),
    ...(duplicateStrategy ? { duplicate_strategy: duplicateStrategy } : {}),
    ...(displayName ? { display_name: displayName } : {}),
    ...(autoExtract !== undefined ? { auto_extract: autoExtract } : {}),
  });
}

export async function enqueueArchiveExtract(
  path: string,
  taskId: string,
  outputDirectory?: string,
): Promise<ArchiveJob> {
  return engine<ArchiveJob>("archive_extract_job", {
    path,
    task_id: taskId,
    ...(outputDirectory ? { output_directory: outputDirectory } : {}),
  });
}

export async function startTask(task: Pick<Task, "id" | "revision">): Promise<Task> {
  return engine<Task>("download_task", { id: task.id });
}

export type TaskAction = "pause" | "resume" | "stop" | "restart";

export async function pauseTask(task: Pick<Task, "id" | "revision">): Promise<Task> {
  return engine<Task>("pause_task", { id: task.id, expected_revision: task.revision });
}

export async function resumeTask(task: Pick<Task, "id" | "revision">): Promise<Task> {
  return engine<Task>("resume_task", { id: task.id, expected_revision: task.revision });
}

export async function stopTask(task: Pick<Task, "id" | "revision">): Promise<Task> {
  return engine<Task>("cancel_task", { id: task.id, expected_revision: task.revision });
}

export async function restartTask(task: Pick<Task, "id" | "revision">): Promise<Task> {
  return engine<Task>("retry_task", { id: task.id, expected_revision: task.revision });
}

export async function deleteTask(id: string, deleteFiles: boolean = false): Promise<{ deleted?: string; affected_ids: string[]; skipped: Array<{ id: string; reason: string }>; revision?: number }> {
  return engine<{ deleted?: string; affected_ids: string[]; skipped: Array<{ id: string; reason: string }>; revision?: number }>("delete_task", { id, delete_files: deleteFiles });
}

export async function deleteTasks(ids: string[], deleteFiles: boolean = false): Promise<{ deleted?: string; affected_ids: string[]; skipped: Array<{ id: string; reason: string }>; revision?: number }> {
  return engine<{ deleted?: string; affected_ids: string[]; skipped: Array<{ id: string; reason: string }>; revision?: number }>("delete_task", { ids, delete_files: deleteFiles });
}

export async function setTaskOptions(task: Pick<Task, "id" | "revision">, options: Record<string, unknown>): Promise<Task> {
  return engine<Task>("set_task_options", { id: task.id, expected_revision: task.revision, ...options });
}

export async function retryFailedTasks(): Promise<Task[]> {
  return engine<Task[]>("retry_failed");
}

export async function pickDirectory(): Promise<string | null> {
  return invoke<string | null>("pick_directory");
}

export async function getDownloadDirectory(): Promise<string | null> {
  const result = await engine<{ path?: string }>("get_download_directory");
  return result.path || null;
}

export async function putSecret(name: string, value: string, kind = "opaque"): Promise<{ credential_ref: string }> {
  return engine<{ credential_ref: string }>("secret_put", { name, value, kind });
}

export { formatBytes, formatSpeed, formatPercent, formatDuration } from "./lib/format";

export type CaptchaType = "image_text" | "positional_click" | "recaptcha_v2" | "recaptcha_v3" | "hcaptcha" | "turnstile" | "browser_session";

export type CaptchaChallenge = {
  id: string;
  task_id?: string | null;
  provider_id: string;
  captcha_type: CaptchaType | string;
  params: Record<string, any>;
  timeout_seconds: number;
  created_at: number;
  expires_at: number;
  time_remaining: number;
  status: string;
  solution?: Record<string, any> | null;
  solver_used?: string | null;
  error?: string | null;
  loopback_url?: string | null;
  is_active_in_ui?: boolean;
  /** The engine-owned lifecycle; commands name its generation. */
  lifecycle?: ChallengeLifecycle;
};

export type ChallengeLifecycle = {
  state: "detected" | "parked" | "routing" | "solving" | "manual_required" | "verifying" | "resolved" | "rejected" | "expired" | "skipped" | "cancelled";
  generation: number;
  reason: string;
  expires_at: number;
  next_action: "wait" | "answer" | "none";
};

const REFUSED: Record<string, string> = {
  stale: "That captcha was replaced by a newer attempt; answer the current one",
  duplicate: "An answer is already being checked with the site",
  closed: "That captcha is already finished",
  unknown_challenge: "That captcha no longer exists",
};

/** A command the engine refused for this attempt, as a readable error. */
function refusedCommand<T extends { outcome?: string }>(result: T): T {
  if (result && result.outcome && REFUSED[result.outcome]) throw new Error(REFUSED[result.outcome]);
  return result;
}

export type CaptchaSolverConfig = {
  id: string;
  name: string;
  priority: number;
  enabled: boolean;
  has_key?: boolean;
  masked_key?: string;
  api_key?: string;
  speech_service?: string;
};

export type CaptchaConfig = {
  solvers: CaptchaSolverConfig[];
  sound_enabled: boolean;
  auto_skip_timeout: boolean;
};

// Unified solveCaptcha with overloads
export async function solveCaptcha(challengeId: string, solverId?: string): Promise<any>;
export async function solveCaptcha(challengeId: string, solution: Record<string, any>, generation?: number): Promise<{ success: boolean; challenge_id: string }>;
export async function solveCaptcha(challengeId: string, arg2?: any, generation?: number): Promise<any> {
  if (typeof arg2 === "string") {
    // UI call with explicit solverId
    return engine("captcha_solve", { challenge_id: challengeId, solver_id: arg2 });
  }
  if (arg2 && typeof arg2 === "object") {
    // Called with a solution object (legacy App.tsx path)
    return refusedCommand(await engine<{ outcome?: string }>("captcha_solve", { challenge_id: challengeId, solution: arg2, generation }));
  }
  // Default: trigger automated solver (clearcote)
  return engine("captcha_solve", { challenge_id: challengeId, solver_id: "clearcote" });
}

export async function solveMultipartCaptcha(groupId: string, host: string, pageUrl: string): Promise<Record<string, unknown>> {
  try {
    return await invoke<Record<string, unknown>>("solve_multipart_captcha", { groupId, host, pageUrl });
  } catch {
    return await engine<Record<string, unknown>>("solve_multipart_captcha", { group_id: groupId, host, page_url: pageUrl });
  }
}

/** Opens the challenge's own page in the browser with a single-use handoff ticket;
 *  the extension brings the answer back. Refusals come back as readable errors. */
/** Finished downloads as the engine recorded them; kept after they leave the Downloads list. */
export async function getHistory(limit = 2000): Promise<Task[]> {
  return engine<Task[]>("history_list", { limit });
}

/** Clears History (all, or the given downloads); the downloads themselves are untouched. */
export async function clearHistory(taskIds?: string[]): Promise<{ removed: number }> {
  return engine<{ removed: number }>("history_clear", taskIds ? { task_ids: taskIds } : {});
}

export async function openCaptchaInBrowser(challengeId: string, generation?: number): Promise<void> {
  const result = await engine<{ outcome: string; url?: string; detail?: string }>("captcha_handoff_open", { challenge_id: challengeId, generation });
  if (result.outcome !== "opened" || !result.url) {
    throw new Error(result.outcome === "unavailable" ? `Can't open this one in the browser: ${result.detail}` : REFUSED[result.detail ?? ""] ?? `Refused: ${result.detail}`);
  }
  await openPath(result.url);
}

export async function skipCaptcha(challengeId: string, scope: "single" | "host" = "single", generation?: number): Promise<{ success: boolean; challenge_id: string; scope: string }> {
  return refusedCommand(await engine<{ success: boolean; challenge_id: string; scope: string; outcome?: string }>("captcha_skip", { challenge_id: challengeId, scope, generation }));
}

export async function getActiveCaptcha(): Promise<CaptchaChallenge | null> {
  return engine<CaptchaChallenge | null>("captcha_get_active_ui");
}

export async function getCaptchaConfig(): Promise<CaptchaConfig> {
  return engine<CaptchaConfig>("captcha_get_config");
}

export async function saveCaptchaConfig(config: Partial<Omit<CaptchaConfig, "solvers">> & {
  solvers?: Array<Partial<CaptchaSolverConfig> & Pick<CaptchaSolverConfig, "id">>;
}): Promise<CaptchaConfig> {
  return engine<CaptchaConfig>("captcha_set_config", { config });
}

export async function checkCaptchaBalance(solverId: string): Promise<{ solver_id: string; balance: number }> {
  return engine<{ solver_id: string; balance: number }>("captcha_check_balance", { solver_id: solverId });
}

export type CaptchaOutcome = "solved" | "failed" | "stalled" | "skipped" | "pending";
export type CaptchaVendor = "cloudflare" | "google" | "hcaptcha" | "image";

export type CaptchaHistoryEntry = {
  id: string;
  task_id?: string | null;
  task_name?: string | null;
  provider_id?: string | null;
  host?: string | null;
  captcha_type: string;
  type_label: string;
  vendor: CaptchaVendor;
  status: string;
  outcome: CaptchaOutcome;
  solver_id?: string | null;
  solver_label?: string | null;
  error?: string | null;
  created_at?: number | null;
  resolved_at?: number | null;
  duration_seconds?: number | null;
  retry_count: number;
  page_url?: string | null;
  group_id?: string | null;
};

export type ShortlinkHistoryHop = {
  hop: number;
  host: string;
  provider?: string | null;
  state?: string | null;
  error?: string | null;
  at?: number | null;
};

export type ShortlinkHistoryChain = {
  task_id: string;
  task_name?: string | null;
  task_state?: string | null;
  updated_at: number;
  failed: boolean;
  hops: ShortlinkHistoryHop[];
};

export type TimelineTone = "success" | "error" | "warning" | "neutral";

export type TaskTimelineEvent = {
  at: number;
  kind: "stage" | "captcha" | "shortlink" | "provider" | "state" | "log";
  title: string;
  tone: TimelineTone;
  detail?: string | null;
  duration_seconds?: number | null;
  stalled?: boolean;
  stage?: string;
  captcha?: CaptchaHistoryEntry;
  hop?: { hop: number; host?: string | null; state?: string | null; provider?: string | null };
};

export type TaskTimeline = {
  task_id: string;
  state: string;
  error?: string | null;
  started_at?: number | null;
  finished_at?: number | null;
  duration_seconds?: number | null;
  captchas: { total: number; solved: number; failed: number };
  shortlink_hops: number;
  stalled_at?: string | null;
  events: TaskTimelineEvent[];
};

export type TaskActivityCounts = Record<string, { captchas: number; captchas_failed: number; hops: number }>;

export async function getCaptchaHistory(limit = 200): Promise<CaptchaHistoryEntry[]> {
  return engine<CaptchaHistoryEntry[]>("captcha_history", { limit });
}

export async function getShortlinkHistory(limit = 30): Promise<ShortlinkHistoryChain[]> {
  return engine<ShortlinkHistoryChain[]>("shortlink_history", { limit });
}

export async function getTaskTimeline(id: string): Promise<TaskTimeline> {
  return engine<TaskTimeline>("task_timeline", { id });
}

export async function getTaskActivityCounts(ids: string[]): Promise<TaskActivityCounts> {
  return engine<TaskActivityCounts>("task_activity_counts", { ids });
}

export type MediaFormatOption = {
  format_id: string;
  ext: string;
  url: string;
  height?: number | null;
  resolution: string;
  fps?: number | null;
  filesize?: number | null;
  vcodec?: string;
  acodec?: string;
  is_video: boolean;
  is_audio: boolean;
  protocol: string;
};

export type MediaExtractionResult = {
  source_url: string;
  title: string;
  duration?: number | null;
  thumbnail?: string | null;
  selected_url: string;
  formats: MediaFormatOption[];
  resolved_item?: Record<string, unknown> | null;
};

export async function extractMedia(url: string): Promise<MediaExtractionResult> {
  return engine<MediaExtractionResult>("media_extract", { url });
}

export type FlareSolverrStatus = {
  available: boolean;
  endpoint: string;
  health?: Record<string, unknown> | null;
};

export async function checkFlareSolverrHealth(): Promise<FlareSolverrStatus> {
  return engine<FlareSolverrStatus>("flaresolverr_check_health");
}

export async function configureFlareSolverr(endpoint: string): Promise<{ configured: boolean; endpoint: string; available: boolean }> {
  return engine<{ configured: boolean; endpoint: string; available: boolean }>("flaresolverr_configure", { endpoint });
}

export type ClearcoteStatus = {
  installed: boolean;
  executable?: string | null;
  version?: string | null;
  size_mb: number;
  can_run: boolean;
};

export async function checkClearcoteStatus(): Promise<ClearcoteStatus> {
  return engine<ClearcoteStatus>("clearcote_status");
}

export type ClearcoteInstall = {
  running: boolean;
  percent: number;
  message: string;
  error: string | null;
  status: ClearcoteStatus | null;
};

/** Starts the Clearcote download in the engine (or returns the one already running). */
export async function startClearcoteInstall(): Promise<ClearcoteInstall> {
  return engine<ClearcoteInstall>("clearcote_install_start");
}

/** The running install's real progress, as the engine measures it. */
export async function getClearcoteInstall(): Promise<ClearcoteInstall> {
  return engine<ClearcoteInstall>("clearcote_install_progress");
}

export async function uninstallClearcote(): Promise<{ uninstalled: boolean }> {
  return engine<{ uninstalled: boolean }>("clearcote_uninstall");
}

export async function configureAudioSolver(params: { speech_service?: string; wit_api_key?: string; google_api_key?: string; enabled?: boolean }): Promise<{ configured: boolean; speech_service?: string; enabled?: boolean }> {
  return engine<{ configured: boolean; speech_service?: string; enabled?: boolean }>("audio_solver_configure", params);
}

export async function listLinkGrabber(state?: string): Promise<LinkGrabberEntry[]> {
  return engine<LinkGrabberEntry[]>("list_linkgrabber", state ? { state } : {});
}

export async function addLinkGrabber(text: string, source = "manual"): Promise<LinkGrabberEntry[]> {
  return engine<LinkGrabberEntry[]>("linkgrabber_add", { text, source });
}

export async function updateLinkGrabber(id: string, changes: Partial<Pick<LinkGrabberEntry, "state" | "selected" | "title" | "error">>): Promise<LinkGrabberEntry> {
  return engine<LinkGrabberEntry>("linkgrabber_update", { id, ...changes });
}

export async function deleteLinkGrabber(id: string): Promise<{ deleted: string }> {
  return engine<{ deleted: string }>("linkgrabber_delete", { id });
}

export async function deleteLinkGrabberMany(ids: string[]): Promise<{ deleted: number }> {
  return engine<{ deleted: number }>("linkgrabber_bulk_delete", { ids });
}

export async function retryLinkGrabber(ids: string[] = []): Promise<{ retried: number }> {
  return engine<{ retried: number }>("linkgrabber_retry", ids.length ? { ids } : {});
}

export async function clearLinkGrabber(state?: string): Promise<{ cleared: boolean }> {
  return engine<{ cleared: boolean }>("linkgrabber_clear", state ? { state } : {});
}

export async function enqueueLinkGrabber(ids: string[], destination?: string): Promise<Task[]> {
  return engine<Task[]>("linkgrabber_enqueue", { ids, destination });
}

export async function setClipboardWatcher(enabled: boolean): Promise<{ enabled: boolean }> {
  return engine<{ enabled: boolean }>("clipboard_set_enabled", { enabled });
}

export async function setClipboardAutostart(enabled: boolean, consent: boolean): Promise<{ enabled: boolean; consent: boolean; supported: boolean }> {
  return engine<{ enabled: boolean; consent: boolean; supported: boolean }>("clipboard_set_autostart", { enabled, consent });
}

export async function setAppAutostart(enabled: boolean): Promise<{ enabled: boolean; supported: boolean }> {
  return engine<{ enabled: boolean; supported: boolean }>("app_set_autostart", { enabled });
}

export async function getClipboardAutostart(): Promise<{ enabled: boolean; consent: boolean; supported: boolean }> {
  return engine<{ enabled: boolean; consent: boolean; supported: boolean }>("clipboard_get_autostart");
}

export async function bulkUpdateLinks(ids: string[], changes: Record<string, unknown>): Promise<{ updated: number; ids: string[] }> {
  return engine<{ updated: number; ids: string[] }>("linkgrabber_bulk_update", { ids, ...changes });
}

export async function inspectLink(id: string, resolve = false): Promise<Record<string, unknown>> {
  return engine<Record<string, unknown>>(resolve ? "linkgrabber_resolve" : "linkgrabber_inspect", { id });
}

export type IntakeKind = "hoster" | "folder" | "file" | "page" | "shortlink" | "unsupported" | "unknown";

export type IntakePageElement = MatrixElement & { provider_id?: string | null; unsupported?: boolean };

export type IntakeAnalysis = {
  url: string;
  kind: IntakeKind;
  provider_id?: string | null;
  provider_name?: string | null;
  message?: string;
  redirected_from?: string;
  via_shortlink?: { url: string; hops: number };
  file?: { name?: string | null; size?: number | null; mime?: string | null };
  page?: {
    url: string;
    title: string;
    favicon?: string | null;
    total_elements: number;
    ads_stripped?: number;
    used_headless?: boolean;
    has_countdown_timer?: boolean;
    elements: IntakePageElement[];
    save_as: string;
    error?: string;
  };
};

/** Work out what a pasted link is: a file, a file-host link, a folder, or a web page. */
export async function analyzeLink(url: string): Promise<IntakeAnalysis> {
  return engine<IntakeAnalysis>("intake_analyze", { url });
}

export async function inspectUrl(url: string): Promise<Record<string, unknown>> {
  return engine<Record<string, unknown>>("inspect_url", { url });
}

export async function enumerateProvider(url: string): Promise<TransferTreeItem[]> {
  return engine<TransferTreeItem[]>("enumerate", { url });
}

export async function listAccounts(providerId?: string): Promise<ProviderAccount[]> {
  return engine<ProviderAccount[]>("list_accounts", providerId ? { provider_id: providerId } : {});
}

export async function createAccount(params: {
  provider_id: string;
  label: string;
  account_type?: string;
  secret_value: string;
  quota_bytes?: number;
}): Promise<ProviderAccount> {
  return engine<ProviderAccount>("account_create", params);
}

export async function saveAccount(account: Partial<ProviderAccount> & { id: string; provider_id: string; credential_ref: string }): Promise<ProviderAccount> {
  return engine<ProviderAccount>("save_account", account as Record<string, unknown>);
}

export async function deleteAccount(accountId: string): Promise<{ deleted: string | null }> {
  return engine<{ deleted: string | null }>("account_delete", { account_id: accountId });
}

export async function selectAccount(providerId: string, accountId?: string): Promise<ProviderAccount | null> {
  return engine<ProviderAccount | null>("account_select", { provider_id: providerId, account_id: accountId });
}

export async function accountQuota(accountId: string, providerId?: string): Promise<Record<string, unknown>> {
  return engine<Record<string, unknown>>("account_quota", { account_id: accountId, provider_id: providerId });
}

export async function refreshAccount(accountId: string, providerId?: string): Promise<ProviderAccount> {
  return engine<ProviderAccount>("refresh_account", { account_id: accountId, provider_id: providerId });
}

export async function getGoogleOAuthUrl(clientId?: string): Promise<{ auth_url: string; client_id: string; scope: string }> {
  return engine<{ auth_url: string; client_id: string; scope: string }>("google_oauth_get_auth_url", clientId ? { client_id: clientId } : {});
}

export type ShortlinkHop = {
  hop_number: number;
  input_url: string;
  output_url: string;
  strategy: string;
  delay_seconds?: number;
  captcha_encountered?: boolean;
  status: string;
  error?: string | null;
};

export async function resolveShortlinkChain(
  url: string,
  taskId?: string,
  method?: "GET" | "POST",
  formData?: Record<string, string> | null
): Promise<{ final_url: string; hops: ShortlinkHop[] }> {
  return engine<{ final_url: string; hops: ShortlinkHop[] }>("shortlink_resolve", {
    url,
    task_id: taskId,
    method: method || "GET",
    form_data: formData || null,
  });
}


export async function importWireguardConfig(confText: string): Promise<{
  interface: Record<string, string>;
  peer: Record<string, string>;
  endpoint: string;
  address?: string;
  dns?: string;
}> {
  return engine<any>("route_import_wireguard", { conf_text: confText });
}

/** Saves a WireGuard location from its .conf; the engine keeps the keys in
 *  the secret store and runs the tunnel inside the transfer core. */
export async function saveWireguardRoute(id: string, confText: string, region: string): Promise<RouteProfile> {
  return engine<RouteProfile>("route_save_wireguard", { id, conf_text: confText, region });
}

/** Account-number sign-in (Mullvad): the engine registers a key and adds one location per city. */
export async function providerAccountSignIn(provider: string, accountNumber: string): Promise<{ locations: number; device_name?: string | null }> {
  return engine("provider_account_login", { provider, account_number: accountNumber });
}

export async function providerAccountRefresh(provider: string): Promise<{ locations: number }> {
  return engine("provider_account_refresh", { provider });
}

/** Removes the account's locations and frees its device slot. */
export async function providerAccountSignOut(provider: string): Promise<{ removed: number }> {
  return engine("provider_account_logout", { provider });
}

export interface AdblockStatus {
  lists: { name: string; bytes: number | null; updated_at: number | null }[];
  builtin_only: boolean;
  cache_bytes: number | null;
}

export async function getAdblockStatus(): Promise<AdblockStatus> {
  return engine<AdblockStatus>("adblock_status", {});
}

/** Fetches the filter lists now; each list reports ok or why it failed. */
export async function updateFilterLists(): Promise<Record<string, { ok: boolean; bytes?: number; error?: string }>> {
  return engine("adblock_update", {});
}

export interface TunnelStatus { socks: string; endpoint: string; handshake_age: number | null; tx_bytes: number | null; rx_bytes: number | null }

/** Tunnels running in the core, by route id. */
export async function getTunnelStatus(): Promise<Record<string, TunnelStatus>> {
  return engine<Record<string, TunnelStatus>>("tunnel_status", {});
}

export async function listRouteProfiles(): Promise<RouteProfile[]> {
  return engine<RouteProfile[]>("routes_list");
}

export async function saveRouteProfile(profile: {
  id: string;
  kind: string;
  endpoint?: string | null;
  region?: string | null;
  healthcheck_url?: string | null;
  enabled?: boolean;
  credential_ref?: string | null;
}): Promise<RouteProfile> {
  return engine<RouteProfile>("route_save", profile);
}

/** Links in pasted or typed text, by the engine's parser (adds https:// to scheme-less links). */
export async function extractLinks(text: string): Promise<string[]> {
  return (await engine<{ urls: string[] }>("urls_extract", { text })).urls;
}

/** Cached site icons as data URLs, fetched once per host by the engine. */
export async function getFavicons(hosts: string[]): Promise<Record<string, string | null>> {
  return (await engine<{ icons: Record<string, string | null> }>("favicons_get", { hosts })).icons;
}

/** The engine's pick for the next healthy route (same rules as quota switching). */
export async function nextHealthyRoute(current?: string | null): Promise<{ route: RouteProfile | null }> {
  return engine<{ route: RouteProfile | null }>("route_next_healthy", current ? { current } : {});
}

export async function deleteRouteProfile(profileId: string): Promise<{ deleted: string }> {
  return engine<{ deleted: string }>("route_delete", { profile_id: profileId });
}

export async function checkRouteHealth(profileId: string): Promise<{
  profile_id: string;
  healthy: boolean;
  checked_at: number;
  public_ip?: string | null;
  error?: string | null;
}> {
  return engine<any>("route_health", { profile_id: profileId });
}

export async function listQuotaLocks(taskId?: string): Promise<Array<{
  task_id: string;
  provider_id: string;
  source_url: string;
  route_profile_id: string;
  outcome: string;
  reason?: string;
}>> {
  return engine<any>("provider_list_quota_locks", taskId ? { task_id: taskId } : {});
}

export async function detectSystemVpns(force: boolean = false): Promise<{
  adapters: Array<{ name: string; description: string; status: string }>;
}> {
  return engine<any>("vpn_detect_adapters", force ? { force: true } : {});
}

export interface PublicProxyItem {
  ip: string;
  port: string;
  protocols: string[];
  country: string;
  city?: string;
  responseTime: number;
  anonymityLevel: string;
  upTime: number;
}

export async function fetchPublicProxies(limit: number = 50, protocol?: string): Promise<PublicProxyItem[]> {
  return engine<PublicProxyItem[]>("routes_fetch_public_proxies", { limit, protocol });
}

export async function testProxyDownload(endpoint: string, kind: string = "http_proxy", credentialRef?: string | null): Promise<{
  ok: boolean;
  latency_ms: number;
  egress_ip?: string;
  status_code?: number;
  bytes_received?: number;
  error?: string;
}> {
  return engine<any>("routes_test_proxy_download", { endpoint, kind, ...(credentialRef ? { credential_ref: credentialRef } : {}) });
}

export async function listBandwidthProfiles(): Promise<BandwidthProfile[]> {
  return engine<BandwidthProfile[]>("list_bandwidth_profiles");
}

export async function setBandwidthRate(rateBytesPerSecond: number): Promise<{ rate_bytes_per_second: number }> {
  return engine<{ rate_bytes_per_second: number }>("set_bandwidth_rate", { rate_bytes_per_second: rateBytesPerSecond });
}

export async function getBandwidthAdmission(params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  return engine<Record<string, unknown>>("bandwidth_admission", params);
}

export async function getAdaptiveTransferStatus(): Promise<AdaptiveTransferStatus> {
  return engine<AdaptiveTransferStatus>("adaptive_transfer_status");
}

export async function bulkTransferAction(action: "pause" | "resume" | "stop"): Promise<BulkTransferActionResult> {
  const method = action === "pause" ? "tasks_pause_all" : action === "resume" ? "tasks_resume_all" : "tasks_stop_all";
  return engine<BulkTransferActionResult>(method);
}

export async function getUiSnapshot(sections?: string[]): Promise<UiSnapshot> {
  return engine<UiSnapshot>("ui_snapshot", sections?.length ? { sections } : {});
}

export async function getUiSettings(): Promise<UiSettings> {
  return engine<UiSettings>("ui_settings_get");
}

export async function updateUiSettings(changes: Partial<UiSettings>): Promise<SettingsUpdateResult> {
  return engine<SettingsUpdateResult>("ui_settings_update", { settings: changes });
}

export async function listNotifications(): Promise<NotificationSink[]> {
  return engine<NotificationSink[]>("list_notification_sinks");
}

export async function saveNotification(sink: NotificationSink): Promise<NotificationSink> {
  return engine<NotificationSink>("save_notification_sink", sink as unknown as Record<string, unknown>);
}

export async function deleteNotification(id: string): Promise<{ deleted: string }> {
  return engine<{ deleted: string }>("delete_notification_sink", { id });
}

export async function clearCompletedTasks(): Promise<{ deleted: number; affected_ids: string[] }> {
  return engine<{ deleted: number; affected_ids: string[] }>("clear_completed_tasks");
}

export async function setPluginEnabled(pluginId: string, enabled: boolean): Promise<{ plugin_id: string; enabled?: boolean }> {
  return engine<{ plugin_id: string; enabled?: boolean }>("set_plugin_state", { plugin_id: pluginId, enabled });
}

export async function openPath(path: string): Promise<void> {
  await invoke("open_path", { path });
}

export type EventEnvelope = {
  id: number;
  event_id: string;
  event_type: string;
  task_id?: string | null;
  resource_id?: string | null;
  created_at?: number;
  payload: Record<string, unknown>;
};

export type ArchiveJob = {
  id: string;
  task_id: string;
  input_path: string;
  output_directory: string;
  format?: string | null;
  state: "queued" | "running" | "completed" | "failed" | "canceled" | string;
  progress_bytes: number;
  error?: string | null;
  operation: "extract" | "join_verify" | string;
  created_at: number;
  updated_at: number;
  revision?: number;
};

export async function getEventsSince(afterId: number = 0, limit: number = 100): Promise<EventEnvelope[]> {
  return engine<EventEnvelope[]>("list_events", { after_id: afterId, limit });
}

export async function acknowledgeEvent(id: number | number[]): Promise<{ acknowledged: number | number[] }> {
  if (Array.isArray(id)) {
    return engine<{ acknowledged: number[] }>("ack_event", { event_ids: id });
  }
  return engine<{ acknowledged: number }>("ack_event", { id });
}

export async function listArchiveJobs(taskId?: string): Promise<ArchiveJob[]> {
  return engine<ArchiveJob[]>("list_archive_jobs", taskId ? { task_id: taskId } : {});
}

export type PythonEnvironmentStatus = {
  installed: boolean;
  version: string | null;
  sidecar_available: boolean;
  can_run: boolean;
};

export async function checkPythonEnvironment(): Promise<PythonEnvironmentStatus> {
  return invoke<PythonEnvironmentStatus>("check_python_environment");
}

export async function installPythonWinget(): Promise<string> {
  return invoke<string>("install_python_winget");
}

export type MatrixElement = {
  id: string;
  tag: "a" | "button" | "form";
  text: string;
  target_url: string;
  host: string;
  provider_id?: string | null;
  score: number;
  category: "high_utility" | "candidate" | "secondary";
  is_shortlink: boolean;
  method?: "GET" | "POST";
  form_data?: Record<string, string> | null;
  filename_hint?: string;
  size_hint?: string;
  selected?: boolean;
  favicon?: string;
  page_title?: string;
  is_multipart?: boolean;
  multipart_base?: string;
  multipart_part?: number;
  multipart_total?: number;
  multipart_format?: string;
  package_name?: string;
  /** What the target file is (engine file category), separate from `category`'s usefulness tier. */
  file_kind?: FileCategory;
};

export type CrawlPageMatrixResponse = {
  url: string;
  title: string;
  favicon?: string;
  total_elements: number;
  ads_stripped: number;
  used_headless: boolean;
  has_countdown_timer: boolean;
  elements: MatrixElement[];
};

export async function crawlPageMatrix(url: string, forceHeadless: boolean = false): Promise<CrawlPageMatrixResponse> {
  return engine<CrawlPageMatrixResponse>("crawl_page_matrix", { url, force_headless: forceHeadless });
}

export type ShortlinkResolutionResult = {
  final_url: string;
  hops: ShortlinkHop[];
  /** Button-following only: the file found, its name, what was passed over and why. */
  direct_url?: string | null;
  filename?: string | null;
  rejected?: { text: string; url: string; reason: string; page: string }[];
  captcha?: boolean;
  error?: string | null;
};

/** Clicks through from a page or button, in the browser with ads blocked, to the file it leads to. */
export async function followDownload(url: string): Promise<ShortlinkResolutionResult> {
  return engine<ShortlinkResolutionResult>("follow_download", { url });
}

export const resolveShortlink = resolveShortlinkChain;

// ─── Telemetry & Dev Logs IPC / RPC ──────────────────────────────────────────

export {
  emitLog,
  type LogLevel,
  type LogErrorDetails,
  type LogEvent,
  type LogQueryOptions,
  type LogQueryResponse,
  type LogExportResponse,
} from "./lib/telemetry";

import { emitLog, type LogQueryOptions, type LogQueryResponse, type LogExportResponse } from "./lib/telemetry";

export async function logsQuery(options?: LogQueryOptions): Promise<LogQueryResponse> {
  return engine<LogQueryResponse>("logs_query", options || {});
}

export async function logsClear(): Promise<{ cleared: boolean }> {
  return engine<{ cleared: boolean }>("logs_clear", {});
}

export async function logsExport(targetPath?: string): Promise<LogExportResponse> {
  return engine<LogExportResponse>("logs_export", targetPath ? { target_path: targetPath } : {});
}

export async function openDevLogsWindow(): Promise<void> {
  return invoke("open_dev_logs_window");
}

export async function closeDevLogsWindow(): Promise<void> {
  return invoke("close_dev_logs_window");
}

export async function toggleDevLogsWindow(): Promise<boolean> {
  return invoke<boolean>("toggle_dev_logs_window");
}

export async function isDevLogsWindowOpen(): Promise<boolean> {
  return invoke<boolean>("is_dev_logs_window_open");
}
