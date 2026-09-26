import {
  MAX_RECONNECT_ATTEMPTS, RECONNECT_BASE_DELAY_MS, RECONNECT_MAX_DELAY_MS,
  PROTOCOL_VERSION, ReplayQueue, createBatchId, frameBatch, frameHello,
  createOpaqueRef, createRequestId, groupKey, isEligibleRequest, normalizeCandidate,
} from "./protocol";
import { createBrowserAdapter, detectBrowserTarget } from "./browserApi";

export type ConnectionState = "disconnected" | "reconnecting" | "connected";

function remoteLog(level: string, event: string, details?: Record<string, unknown>): void {
  try {
    fetch("http://127.0.0.1:8788/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ts: new Date().toISOString(),
        level,
        event,
        details: details ?? {},
      }),
    }).catch(() => {});
  } catch {
    // Ignore collector connectivity errors
  }
}

export class CaptureController {
  readonly queue = new ReplayQueue();
  readonly sessionRef = createOpaqueRef("session");
  readonly adapter;
  state: ConnectionState = "disconnected";
  retryAttempt = 0;
  lastError: string | undefined;
  private port: any;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private readonly stateListeners = new Set<(state: ConnectionState) => void>();
  private readonly candidates = new Map<string, Record<string, unknown>[]>();

  constructor(adapter = createBrowserAdapter(detectBrowserTarget()), private readonly hostName = "ai.transfer.manager.browser") {
    this.adapter = adapter;
    remoteLog("INFO", "controller_constructed", { hostName, target: adapter.target });
  }

  onState(listener: (state: ConnectionState) => void): () => void {
    this.stateListeners.add(listener);
    listener(this.state);
    return () => this.stateListeners.delete(listener);
  }

  private setState(state: ConnectionState): void {
    this.state = state;
    remoteLog("INFO", "state_transition", { state, retryAttempt: this.retryAttempt, lastError: this.lastError });
    const notify = this.adapter.runtime?.sendMessage?.({ type: "capture_state", state });
    if (notify && typeof notify.catch === "function") {
      notify.catch(() => { this.lastError = "capture state notification unavailable"; });
    }
    for (const listener of this.stateListeners) listener(state);
  }

  connect(): void {
    if (this.port) {
      remoteLog("INFO", "connect_skipped_port_already_active", { hostName: this.hostName });
      return;
    }
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = undefined;
    }
    this.setState(this.retryAttempt ? "reconnecting" : "disconnected");
    remoteLog("INFO", "connectNative_attempt", {
      hostName: this.hostName,
      retryAttempt: this.retryAttempt,
      origin: this.adapter.extensionOrigin(),
    });
    try {
      this.port = this.adapter.connectNative(this.hostName);
      remoteLog("INFO", "connectNative_port_created", { hasPort: Boolean(this.port) });
      this.adapter.addListener(this.port.onMessage, (message: Record<string, unknown>) => this.handleMessage(message));
      this.adapter.addListener(this.port.onDisconnect, () => this.disconnected());
      const hello = frameHello(this.adapter.extensionOrigin(), "", this.sessionRef);
      remoteLog("INFO", "posting_hello_frame", { hello });
      this.port.postMessage(hello);
    } catch (exc: any) {
      remoteLog("ERROR", "connect_threw_exception", { error: String(exc), message: exc?.message });
      this.disconnected();
    }
  }

  private disconnected(): void {
    const err = this.adapter.runtime?.lastError;
    if (err) {
      this.lastError = err.message || String(err);
    }
    remoteLog("WARN", "native_port_disconnected", {
      lastError: this.lastError,
      retryAttempt: this.retryAttempt,
      maxAttempts: MAX_RECONNECT_ATTEMPTS,
    });
    this.port = undefined;
    if (this.retryAttempt >= MAX_RECONNECT_ATTEMPTS) {
      this.setState("disconnected");
      remoteLog("ERROR", "max_reconnect_attempts_exhausted", { retryAttempt: this.retryAttempt });
      return;
    }
    this.setState("reconnecting");
    if (this.retryTimer) return;
    const delay = Math.min(RECONNECT_MAX_DELAY_MS, RECONNECT_BASE_DELAY_MS * 2 ** this.retryAttempt);
    this.retryAttempt += 1;
    remoteLog("INFO", "scheduling_reconnect", { delayMs: delay, nextAttempt: this.retryAttempt });
    this.retryTimer = setTimeout(() => {
      this.retryTimer = undefined;
      this.connect();
    }, delay);
  }

  private handleMessage(message: Record<string, unknown>): void {
    remoteLog("INFO", "native_message_received", { message });
    if (message.version !== PROTOCOL_VERSION) {
      remoteLog("WARN", "protocol_version_mismatch", { expected: PROTOCOL_VERSION, received: message.version });
      return;
    }
    if (message.type === "hello_ack") {
      this.retryAttempt = 0;
      this.setState("connected");
      remoteLog("INFO", "handshake_successful", { connectionId: message.connection_id });
      this.flush();
    } else if (message.type === "challenge_return_ack") {
      const outcome = (message.result as Record<string, unknown> | undefined)?.outcome;
      remoteLog(outcome === "accepted" ? "INFO" : "WARNING", "challenge_return_result", { outcome });
    } else if (message.type === "session_sync_ack") {
      remoteLog("INFO", "session_sync_acknowledged", { result: message.result });
    } else if (message.type === "ack" && typeof message.batch_id === "string" && typeof message.request_id === "string") {
      this.queue.acknowledge(message.batch_id, message.request_id);
    } else if (message.type === "error") {
      remoteLog("ERROR", "native_host_error", { error: message.error, batch_id: message.batch_id });
    }
  }

  syncSession(sessionData: { domain: string; page_url?: string; cookies: Record<string, unknown>[] }): void {
    const pageOrigin = sessionData.page_url ? (() => { try { return new URL(sessionData.page_url).origin; } catch { return ""; } })() : "";
    const frame = {
      version: PROTOCOL_VERSION,
      type: "session_sync",
      request_id: createRequestId(),
      origin: { extension_origin: this.adapter.extensionOrigin(), page_origin: pageOrigin },
      session: {
        domain: sessionData.domain,
        page_url: sessionData.page_url,
        cookies: sessionData.cookies,
        user_agent: typeof navigator !== "undefined" ? navigator.userAgent : "",
      },
    };
    if (this.state === "connected" && this.port) {
      this.port.postMessage(frame);
      remoteLog("INFO", "session_sync_dispatched", { domain: sessionData.domain, count: sessionData.cookies.length });
    } else {
      this.connect();
      setTimeout(() => {
        if (this.port) {
          this.port.postMessage(frame);
          remoteLog("INFO", "session_sync_dispatched_after_connect", { domain: sessionData.domain, count: sessionData.cookies.length });
        }
      }, 500);
    }
  }

  /** Hands a captcha answer back to the app; the engine checks every binding. */
  returnChallenge(handoff: Record<string, unknown>): void {
    const frame = { version: PROTOCOL_VERSION, type: "challenge_return", request_id: createRequestId(), handoff };
    if (this.state !== "connected" || !this.port) this.connect();
    if (this.port) this.port.postMessage(frame);
    remoteLog("INFO", "challenge_return_dispatched", { challenge_id: handoff.challenge_id });
  }

  capture(details: Record<string, unknown>, options: {
    passiveCapture?: boolean;
    explicit?: boolean;
    mediaSelected?: boolean;
  } = {}): void {
    if (!isEligibleRequest(details, options)) return;
    const page = String(details.documentUrl ?? details.originUrl ?? details.pageUrl ?? details.referrer ?? details.url ?? "");
    const key = groupKey(details, this.sessionRef);
    const current = this.candidates.get(key) ?? [];
    current.push(normalizeCandidate(details, this.sessionRef));
    this.candidates.set(key, current);
    if (options.explicit || options.mediaSelected || details.download === true) this.submit(key, page);
  }

  submit(key: string, pageUrl = ""): void {
    const candidates = this.candidates.get(key) ?? [];
    if (!candidates.length) return;
    let pageOrigin = "";
    try {
      if (pageUrl) pageOrigin = new URL(pageUrl).origin;
    } catch {
      pageOrigin = "";
    }
    if (!pageOrigin && candidates.length > 0 && candidates[0].url) {
      try {
        const candidateUrl = new URL(String(candidates[0].url));
        pageOrigin = candidateUrl.origin;
        if (!pageUrl) pageUrl = candidateUrl.href;
      } catch {
        // invalid candidate url
      }
    }
    const batch = frameBatch({
      version: PROTOCOL_VERSION, type: "candidate_batch", batch_id: createBatchId(), request_id: createRequestId(),
      origin: { extension_origin: this.adapter.extensionOrigin(), page_origin: pageOrigin },
      page: { url: pageUrl }, session_ref: this.sessionRef, candidates,
    });
    this.queue.enqueue(batch);
    this.incrementCapturedCount(candidates.length);
    this.flush();
    this.candidates.delete(key);
  }

  private incrementCapturedCount(count: number): void {
    try {
      this.adapter.storage?.local?.get?.("stats", (data: any) => {
        const current = Number(data?.stats?.capturedCount ?? 0) + count;
        this.adapter.storage?.local?.set?.({ stats: { capturedCount: current } });
      });
    } catch {
      // Ignore storage errors
    }
  }

  private flush(): void {
    if (this.state !== "connected" || !this.port) return;
    for (const batch of this.queue.pending()) this.port.postMessage(batch);
  }
}

function showNotification(adapter: Record<string, any>, title: string, message: string): void {
  try {
    const iconUrl = adapter.runtime?.getURL ? adapter.runtime.getURL("icons/icon-48.png") : "icons/icon-48.png";
    adapter.notifications?.create?.({
      type: "basic",
      iconUrl,
      title,
      message,
    }, () => void adapter.runtime?.lastError);
  } catch {
    // Notification API might be restricted or unsupported
  }
}

function extractUrlsFromText(text: string): string[] {
  const matches = text.match(/https?:\/\/[^\s"'<>]+/gi) || [];
  return Array.from(new Set(matches));
}

function captureAndSyncSession(
  controller: CaptureController,
  tabUrl: string,
  storeId?: string,
  callback?: (res: { success: boolean; count: number; domain: string; message?: string }) => void
): void {
  try {
    const url = new URL(tabUrl);
    if (!["http:", "https:"].includes(url.protocol)) {
      callback?.({ success: false, count: 0, domain: "", message: "Unsupported protocol" });
      return;
    }
    const query: Record<string, any> = { url: tabUrl };
    if (storeId) {
      query.storeId = storeId;
    }
    controller.adapter.cookies?.getAll?.(query, (cookies: any[]) => {
      void controller.adapter.runtime?.lastError;
      const cookieList = (cookies || []).map((c: any) => ({
        name: String(c.name || ""),
        value: String(c.value || ""),
        domain: String(c.domain || url.hostname),
        path: String(c.path || "/"),
        secure: Boolean(c.secure),
        httpOnly: Boolean(c.httpOnly),
        expirationDate: typeof c.expirationDate === "number" ? c.expirationDate : undefined,
      }));
      controller.syncSession({
        domain: url.hostname,
        page_url: tabUrl,
        cookies: cookieList,
      });
      showNotification(controller.adapter, "MossDL", `Captured ${cookieList.length} cookie(s) for ${url.hostname}`);
      callback?.({ success: true, count: cookieList.length, domain: url.hostname });
    });
  } catch (err) {
    callback?.({ success: false, count: 0, domain: "", message: String(err) });
  }
}

function registerContextMenus(adapter: Record<string, any>): void {
  if (!adapter.contextMenus?.create) return;
  try {
    adapter.contextMenus.removeAll?.(() => {
      void adapter.runtime?.lastError;
      const parentId = "tm-parent";
      adapter.contextMenus.create({
        id: parentId,
        title: "Download with MossDL",
        contexts: ["all"],
      }, () => void adapter.runtime?.lastError);

      adapter.contextMenus.create({
        id: "tm-download-link",
        parentId,
        title: "Download Link",
        contexts: ["link"],
      }, () => void adapter.runtime?.lastError);

      adapter.contextMenus.create({
        id: "tm-download-selection",
        parentId,
        title: "Download Selection",
        contexts: ["selection"],
      }, () => void adapter.runtime?.lastError);

      adapter.contextMenus.create({
        id: "tm-capture-all-links",
        parentId,
        title: "Capture All Links on Page",
        contexts: ["page"],
      }, () => void adapter.runtime?.lastError);

      adapter.contextMenus.create({
        id: "tm-capture-media",
        parentId,
        title: "Capture Page Media / Video",
        contexts: ["page", "video", "audio", "image"],
      }, () => void adapter.runtime?.lastError);

      adapter.contextMenus.create({
        id: "tm-capture-cookies",
        parentId,
        title: "Capture Cookies of this Session",
        contexts: ["page", "action"],
      }, () => void adapter.runtime?.lastError);
    });
  } catch (err) {
    remoteLog("WARN", "context_menus_registration_failed", { error: String(err) });
  }
}

export function installBackgroundCapture(controller = new CaptureController()): CaptureController {
  const webRequest = controller.adapter.webRequest;
  controller.connect();
  registerContextMenus(controller.adapter);
  controller.adapter.addListener(controller.adapter.runtime?.onInstalled, () => {
    registerContextMenus(controller.adapter);
  });

  if (webRequest?.onBeforeRequest) {
    try {
      controller.adapter.addListener(
        webRequest.onBeforeRequest,
        (details: Record<string, unknown>) => controller.capture(details),
        { urls: ["http://*/*", "https://*/*"] }
      );
    } catch {
      // webRequest might require extra permissions in some contexts
    }
  }

  // Intercept browser downloads
  controller.adapter.addListener(controller.adapter.downloads?.onCreated, (details: Record<string, unknown>) => {
    remoteLog("INFO", "download_created", { id: details.id, url: details.url, filename: details.filename });
    controller.adapter.storage?.local?.get?.("preferences", (data: any) => {
      const intercept = data?.preferences?.interceptDownloads !== false;
      const downloadId = typeof details.id === "number" ? details.id : undefined;
      if (intercept && downloadId !== undefined) {
        const erase = () => {
          try {
            const erasePromise = controller.adapter.downloads?.erase?.({ id: downloadId }, () => {
              void controller.adapter.runtime?.lastError;
            });
            if (erasePromise && typeof erasePromise.catch === "function") {
              erasePromise.catch(() => {});
            }
          } catch {
            // Ignore erase failure
          }
        };

        try {
          const cancelPromise = controller.adapter.downloads?.cancel?.(downloadId, () => {
            void controller.adapter.runtime?.lastError;
            erase();
          });
          if (cancelPromise && typeof cancelPromise.then === "function") {
            cancelPromise.then(() => erase()).catch((err: any) => {
              remoteLog("WARN", "download_cancel_failed", { id: downloadId, error: String(err) });
            });
          }
        } catch (err) {
          remoteLog("WARN", "download_cancel_failed", { id: downloadId, error: String(err) });
        }
        const fname = String(details.filename || details.url || "file");
        showNotification(controller.adapter, "MossDL", `Intercepted download: ${fname}`);
      }
      controller.capture({ ...details, method: "GET", download: true }, { explicit: true });
    });
  });

  // Handle context menu clicks
  controller.adapter.addListener(controller.adapter.contextMenus?.onClicked, (info: any, tab: any) => {
    remoteLog("INFO", "context_menu_clicked", { menuItemId: info?.menuItemId, pageUrl: tab?.url });
    if (info?.menuItemId === "tm-download-link" && info?.linkUrl) {
      controller.capture({
        url: info.linkUrl,
        pageUrl: tab?.url,
        documentUrl: tab?.url,
        method: "GET",
        download: true,
      }, { explicit: true });
      showNotification(controller.adapter, "MossDL", `Captured link: ${info.linkUrl}`);
    } else if (info?.menuItemId === "tm-download-selection" && info?.selectionText) {
      const urls = extractUrlsFromText(String(info.selectionText));
      if (urls.length > 0) {
        for (const url of urls) {
          controller.capture({ url, pageUrl: tab?.url, method: "GET", download: true }, { explicit: true });
        }
        showNotification(controller.adapter, "MossDL", `Captured ${urls.length} link(s) from selection`);
      } else {
        showNotification(controller.adapter, "MossDL", "No URLs found in selection");
      }
    } else if (info?.menuItemId === "tm-capture-all-links" && tab?.id) {
      controller.adapter.tabs?.sendMessage?.(tab.id, { type: "scan_and_capture" }, (response: any) => {
        void controller.adapter.runtime?.lastError;
        const count = Number(response?.capturedCount ?? 0);
        if (count > 0) {
          showNotification(controller.adapter, "MossDL", `Captured ${count} link(s) on page`);
        } else {
          showNotification(controller.adapter, "MossDL", "No links found on this page");
        }
      });
      showNotification(controller.adapter, "MossDL", "Scanning page for download links...");
    } else if (info?.menuItemId === "tm-capture-media" && tab?.id) {
      controller.adapter.tabs?.sendMessage?.(tab.id, { type: "capture_active_media" }, () => void controller.adapter.runtime?.lastError);
    } else if (info?.menuItemId === "tm-capture-cookies" && tab?.url) {
      captureAndSyncSession(controller, tab.url, tab.cookieStoreId);
    }
  });

  // Runtime messaging
  controller.adapter.addListener(controller.adapter.runtime?.onMessage, (message: any, _sender: any, sendResponse: (res?: any) => void) => {
    if (!message || typeof message !== "object") return false;

    if (message.type === "capture_candidate" && message.details && typeof message.details === "object") {
      controller.capture(message.details as Record<string, unknown>, { explicit: true, mediaSelected: true });
      sendResponse?.({ status: "captured" });
      return true;
    }

    if (message.type === "capture_batch" && Array.isArray(message.candidates)) {
      for (const item of message.candidates) {
        if (item && typeof item === "object") {
          controller.capture(item as Record<string, unknown>, { explicit: true });
        }
      }
      sendResponse?.({ status: "batch_captured", count: message.candidates.length });
      return true;
    }

    if (message.type === "challenge_solved" && typeof message.ticket === "string" && typeof message.token === "string") {
      // The origin comes from the tab that sent it, never from the message.
      const senderUrl = typeof _sender?.url === "string" ? _sender.url : "";
      let origin = "";
      try { origin = new URL(senderUrl).origin; } catch { origin = ""; }
      if (!origin) { sendResponse?.({ status: "rejected", reason: "no sender origin" }); return true; }
      const host = new URL(origin).hostname;
      const storeId = _sender?.tab?.cookieStoreId;
      const query: Record<string, unknown> = { url: origin };
      if (storeId) query.storeId = storeId;
      controller.adapter.cookies?.getAll?.(query, (cookies: any[]) => {
        const inScope = (cookies || []).filter((c) => {
          const domain = String(c?.domain || "").replace(/^\./, "").toLowerCase();
          return domain && (host === domain || host.endsWith("." + domain));
        }).slice(0, 50).map((c) => ({ name: String(c.name), value: String(c.value), domain: String(c.domain) }));
        controller.returnChallenge({
          version: "mossdl-handoff/1", ticket: message.ticket, challenge_id: String(message.challengeId),
          generation: Number(message.generation), origin, profile: "default", token: message.token.slice(0, 8192),
          cookies: inScope, user_agent: typeof navigator !== "undefined" ? navigator.userAgent : "",
        });
        sendResponse?.({ status: "returned" });
      });
      return true;
    }

    if (message.type === "sync_session" && typeof message.page_url === "string") {
      captureAndSyncSession(controller, message.page_url, message.store_id, (res) => {
        sendResponse?.(res);
      });
      return true;
    }

    if (message.type === "get_state") {
      if (controller.state !== "connected" && !controller.port) {
        controller.retryAttempt = 0;
        controller.connect();
      }
      controller.adapter.storage?.local?.get?.("stats", (data: any) => {
        sendResponse?.({
          state: controller.state,
          retryAttempt: controller.retryAttempt,
          sessionRef: controller.sessionRef,
          capturedCount: Number(data?.stats?.capturedCount ?? 0),
        });
      });
      return true; // Asynchronous sendResponse
    }

    if (message.type === "reconnect") {
      controller.retryAttempt = 0;
      controller.connect();
      sendResponse?.({ state: controller.state });
      return true;
    }

    return false;
  });

  return controller;
}

if (typeof globalThis !== "undefined" && ((globalThis as any).browser?.runtime?.onStartup || (globalThis as any).chrome?.runtime?.onStartup)) {
  installBackgroundCapture();
}
