export const PROTOCOL_VERSION = "browser-capture/1";
export const MAX_CANDIDATES = 128;
export const MAX_BATCH_BYTES = 2 * 1024 * 1024;
export const MAX_REPLAY_BATCHES = 32;
export const MAX_RECONNECT_ATTEMPTS = 6;
export const RECONNECT_BASE_DELAY_MS = 250;
export const RECONNECT_MAX_DELAY_MS = 4000;

const SECRET_QUERY_KEYS = new Set([
  "sig", "signature", "token", "expires", "expiry", "auth", "authorization",
  "x-amz-signature", "x-amz-credential", "x-amz-security-token",
]);
const SECRET_HEADERS = new Set([
  "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key",
]);
const SAFE_HEADERS = new Set(["accept", "accept-language", "content-type", "range", "referer", "user-agent"]);
const MEDIA_TYPES = new Set(["media", "object", "xmlhttprequest"]);
const MEDIA_MIME_PREFIXES = ["audio/", "video/"];
const MEDIA_SUFFIXES = [".m3u8", ".mpd", ".mp4", ".webm", ".m4a", ".mp3", ".aac", ".ts", ".m4s"];

export function redactUrl(rawUrl: string): string {
  try {
    const url = new URL(rawUrl);
    const safe = new URL(url.origin + url.pathname);
    for (const [key, value] of url.searchParams.entries()) {
      if (!SECRET_QUERY_KEYS.has(key.toLowerCase())) safe.searchParams.append(key, value);
    }
    return safe.toString();
  } catch {
    return "";
  }
}

export function safeHeaders(rawHeaders: unknown): Record<string, string> {
  if (!rawHeaders || typeof rawHeaders !== "object" || Array.isArray(rawHeaders)) return {};
  const result: Record<string, string> = {};
  for (const [key, value] of Object.entries(rawHeaders as Record<string, unknown>)) {
    const lower = key.toLowerCase();
    if (SECRET_HEADERS.has(lower) || !SAFE_HEADERS.has(lower)) continue;
    if (typeof value !== "string") continue;
    result[key] = value.slice(0, 4096);
  }
  return result;
}

export function isMediaCandidate(details: Record<string, unknown>): boolean {
  const mime = String(details.mimeType ?? details.mime ?? "").toLowerCase();
  const url = String(details.url ?? "").toLowerCase().split("?")[0];
  return MEDIA_TYPES.has(String(details.type ?? "").toLowerCase())
    || MEDIA_MIME_PREFIXES.some((prefix) => mime.startsWith(prefix))
    || MEDIA_SUFFIXES.some((suffix) => url.endsWith(suffix));
}

export function isEligibleRequest(details: Record<string, unknown>, options: {
  passiveCapture?: boolean;
  explicit?: boolean;
  mediaSelected?: boolean;
} = {}): boolean {
  const method = String(details.method ?? "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") return false;
  let url: URL;
  try { url = new URL(String(details.url ?? "")); } catch { return false; }
  if (!(["http:", "https:"].includes(url.protocol)) || !url.hostname) return false;
  const media = isMediaCandidate(details);
  const explicit = Boolean(options.explicit || options.mediaSelected || details.download === true);
  return explicit || media || Boolean(options.passiveCapture);
}

export function normalizeCandidate(details: Record<string, unknown>, sessionRef: string): Record<string, unknown> {
  const url = redactUrl(String(details.url ?? ""));
  const pageUrl = String(details.documentUrl ?? details.originUrl ?? details.pageUrl ?? "");
  const filename = String(details.filename ?? details.displayName ?? "").trim() || undefined;
  return {
    url,
    method: String(details.method ?? "GET").toUpperCase(),
    headers: safeHeaders(details.headers),
    filename,
    mime: details.mimeType ?? details.mime,
    size: Number.isFinite(Number(details.size)) ? Number(details.size) : undefined,
    referrer: typeof details.referrer === "string" ? redactUrl(details.referrer) : undefined,
    page_url: pageUrl ? redactUrl(pageUrl) : undefined,
    page_origin: pageUrl ? (() => { try { return new URL(pageUrl).origin; } catch { return ""; } })() : undefined,
    session_ref: sessionRef,
    confidence: isMediaCandidate(details) ? 0.75 : 0.5,
  };
}

export function createOpaqueRef(prefix: string): string {
  const uuid = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${uuid}`;
}

export function createBatchId(): string { return createOpaqueRef("batch"); }
export function createRequestId(): string { return createOpaqueRef("request"); }

export function rankCandidates(candidates: Record<string, unknown>[]): Record<string, unknown>[] {
  return [...candidates].sort((left, right) => {
    const score = Number(right.confidence ?? 0) - Number(left.confidence ?? 0);
    return score || String(left.url ?? "").localeCompare(String(right.url ?? ""));
  }).slice(0, MAX_CANDIDATES);
}

export function groupKey(details: Record<string, unknown>, sessionRef: string): string {
  const page = String(details.documentUrl ?? details.originUrl ?? details.pageUrl ?? "");
  try { return `${sessionRef}:${new URL(page).origin}:${page}`; } catch { return `${sessionRef}:unknown`; }
}

export class ReplayQueue {
  private readonly batches = new Map<string, Record<string, unknown>>();

  enqueue(batch: Record<string, unknown>): void {
    const key = `${String(batch.batch_id)}:${String(batch.request_id)}`;
    this.batches.set(key, batch);
    while (this.batches.size > MAX_REPLAY_BATCHES) {
      const first = this.batches.keys().next().value;
      if (first) this.batches.delete(first);
    }
  }

  acknowledge(batchId: string, requestId: string): void {
    this.batches.delete(`${batchId}:${requestId}`);
  }

  pending(): Record<string, unknown>[] { return [...this.batches.values()]; }
}

export function frameHello(extensionOrigin: string, pageOrigin: string, sessionRef: string): Record<string, unknown> {
  return {
    version: PROTOCOL_VERSION, type: "hello", request_id: createRequestId(),
    origin: { extension_origin: extensionOrigin, page_origin: pageOrigin },
    session_ref: sessionRef, capabilities: ["candidate-batches", "review", "direct-import", "replay"],
  };
}

export function frameBatch(batch: Record<string, unknown>): Record<string, unknown> {
  const candidates = rankCandidates(Array.isArray(batch.candidates) ? batch.candidates as Record<string, unknown>[] : []);
  const frame = { version: PROTOCOL_VERSION, type: "candidate_batch", request_id: batch.request_id,
    batch_id: batch.batch_id, origin: batch.origin, page: batch.page, session_ref: batch.session_ref, candidates };
  if (JSON.stringify(frame).length > MAX_BATCH_BYTES) throw new Error("candidate batch exceeds extension limit");
  return frame;
}
