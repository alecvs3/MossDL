import { createBrowserAdapter } from "./browserApi";

interface PopupPreferences {
  mediaOverlays: boolean;
  interceptDownloads: boolean;
  cleanTracking: boolean;
  streamSniffer: boolean;
}

const DEFAULT_PREFERENCES: PopupPreferences = {
  mediaOverlays: true,
  interceptDownloads: true,
  cleanTracking: true,
  streamSniffer: true,
};

function getElements() {
  return {
    statusPill: document.getElementById("connection-status"),
    statusLabel: document.getElementById("connection-label"),
    btnScanPage: document.getElementById("btn-scan-page") as HTMLButtonElement | null,
    btnCaptureMedia: document.getElementById("btn-capture-media") as HTMLButtonElement | null,
    btnSyncSession: document.getElementById("btn-sync-session") as HTMLButtonElement | null,
    btnOpenDesktop: document.getElementById("btn-open-desktop") as HTMLButtonElement | null,
    scanBadge: document.getElementById("scan-badge"),
    mediaBadge: document.getElementById("media-badge"),
    prefMediaOverlays: document.getElementById("pref-media-overlays") as HTMLInputElement | null,
    prefInterceptDownloads: document.getElementById("pref-intercept-downloads") as HTMLInputElement | null,
    prefCleanTracking: document.getElementById("pref-clean-tracking") as HTMLInputElement | null,
    prefStreamSniffer: document.getElementById("pref-stream-sniffer") as HTMLInputElement | null,
    statCapturedCount: document.getElementById("stat-captured-count"),
    statBridgeStatus: document.getElementById("stat-bridge-status"),
    toastMessage: document.getElementById("toast-message"),
  };
}

function showToast(message: string, durationMs = 2600): void {
  const elements = getElements();
  if (!elements.toastMessage) return;
  elements.toastMessage.textContent = message;
  elements.toastMessage.style.display = "block";
  setTimeout(() => {
    if (elements.toastMessage) elements.toastMessage.style.display = "none";
  }, durationMs);
}

function updateConnectionUI(state: string, retryAttempt = 0): void {
  const { statusPill, statusLabel, statBridgeStatus } = getElements();
  if (!statusPill || !statusLabel) return;

  statusPill.classList.remove("status-connected", "status-reconnecting", "status-disconnected");

  if (state === "connected") {
    statusPill.classList.add("status-connected");
    statusLabel.textContent = "Connected";
    if (statBridgeStatus) statBridgeStatus.textContent = "Connected";
  } else if (state === "reconnecting") {
    statusPill.classList.add("status-reconnecting");
    statusLabel.textContent = retryAttempt > 0 ? `Reconnecting (${retryAttempt})...` : "Reconnecting...";
    if (statBridgeStatus) statBridgeStatus.textContent = "Reconnecting";
  } else {
    statusPill.classList.add("status-disconnected");
    statusLabel.textContent = "Offline";
    if (statBridgeStatus) statBridgeStatus.textContent = "Disconnected";
  }
}

async function loadPreferences(): Promise<PopupPreferences> {
  const adapter = createBrowserAdapter();
  return new Promise((resolve) => {
    try {
      adapter.storage?.local?.get?.("preferences", (data: any) => {
        const stored = data?.preferences as Partial<PopupPreferences> | undefined;
        resolve({ ...DEFAULT_PREFERENCES, ...(stored ?? {}) });
      });
    } catch {
      resolve(DEFAULT_PREFERENCES);
    }
  });
}

function savePreferences(prefs: PopupPreferences): void {
  const adapter = createBrowserAdapter();
  try {
    adapter.storage?.local?.set?.({ preferences: prefs });
  } catch {
    // Gracefully handle storage errors
  }
}

async function initPopup(): Promise<void> {
  const adapter = createBrowserAdapter();
  const elements = getElements();

  // The site this popup acts on, shown in the "This page" card.
  adapter.tabs?.query?.({ active: true, currentWindow: true }, (tabs: any[]) => {
    const hostEl = document.getElementById("page-host");
    let host = "";
    try { host = new URL(String(tabs?.[0]?.url || "")).hostname.replace(/^www\./, ""); } catch { host = ""; }
    if (hostEl) hostEl.textContent = host || "Not a web page";
  });

  // Load preferences
  const prefs = await loadPreferences();
  if (elements.prefMediaOverlays) elements.prefMediaOverlays.checked = prefs.mediaOverlays;
  if (elements.prefInterceptDownloads) elements.prefInterceptDownloads.checked = prefs.interceptDownloads;
  if (elements.prefCleanTracking) elements.prefCleanTracking.checked = prefs.cleanTracking;
  if (elements.prefStreamSniffer) elements.prefStreamSniffer.checked = prefs.streamSniffer;

  const onPrefChange = () => {
    const updated: PopupPreferences = {
      mediaOverlays: elements.prefMediaOverlays?.checked ?? true,
      interceptDownloads: elements.prefInterceptDownloads?.checked ?? true,
      cleanTracking: elements.prefCleanTracking?.checked ?? true,
      streamSniffer: elements.prefStreamSniffer?.checked ?? true,
    };
    savePreferences(updated);
    showToast("Preferences updated");
  };

  elements.prefMediaOverlays?.addEventListener("change", onPrefChange);
  elements.prefInterceptDownloads?.addEventListener("change", onPrefChange);
  elements.prefCleanTracking?.addEventListener("change", onPrefChange);
  elements.prefStreamSniffer?.addEventListener("change", onPrefChange);

  // Status pill click to reconnect
  elements.statusPill?.addEventListener("click", () => {
    updateConnectionUI("reconnecting");
    adapter.runtime?.sendMessage?.({ type: "reconnect" }, () => { void adapter.runtime?.lastError; });
    showToast("Attempting to reconnect native bridge...");
  });

  // Query background connection state
  try {
    adapter.runtime?.sendMessage?.({ type: "get_state" }, (res: any) => {
      if (adapter.runtime?.lastError) {
        updateConnectionUI("disconnected");
        return;
      }
      if (res && res.state) {
        updateConnectionUI(res.state, res.retryAttempt);
      }
      if (res && typeof res.capturedCount === "number" && elements.statCapturedCount) {
        elements.statCapturedCount.textContent = String(res.capturedCount);
      }
    });
  } catch {
    updateConnectionUI("disconnected");
  }

  // Listen for state change broadcasts
  adapter.addListener(adapter.runtime?.onMessage, (message: any) => {
    if (message?.type === "capture_state" && typeof message.state === "string") {
      updateConnectionUI(message.state);
    }
  });

  // Query active tab for inspection
  try {
    adapter.tabs?.query?.({ active: true, currentWindow: true }, (tabs: any[]) => {
      if (adapter.runtime?.lastError || !tabs || tabs.length === 0) return;
      const activeTab = tabs[0];
      if (!activeTab || typeof activeTab.id !== "number") return;
      const tabUrl = String(activeTab.url || "");
      if (tabUrl.startsWith("chrome:") || tabUrl.startsWith("edge:") || tabUrl.startsWith("about:") || tabUrl.startsWith("chrome-extension:") || tabUrl.startsWith("moz-extension:")) {
        return;
      }

      adapter.tabs?.sendMessage?.(activeTab.id, { type: "inspect_tab" }, (response: any) => {
        if (adapter.runtime?.lastError || !response) return;
        if (elements.scanBadge && typeof response.downloadLinksCount === "number") {
          elements.scanBadge.textContent = String(response.downloadLinksCount);
        }
        if (elements.mediaBadge && typeof response.mediaElementsCount === "number") {
          elements.mediaBadge.textContent = String(response.mediaElementsCount);
        }
      });
    });
  } catch {
    // Tab querying may be restricted on internal pages
  }

  // Button: Scan page links
  elements.btnScanPage?.addEventListener("click", () => {
    adapter.tabs?.query?.({ active: true, currentWindow: true }, (tabs: any[]) => {
      if (adapter.runtime?.lastError || !tabs || tabs.length === 0) {
        showToast("Cannot scan current tab");
        return;
      }
      const activeTab = tabs[0];
      if (!activeTab || typeof activeTab.id !== "number") {
        showToast("Cannot scan current tab");
        return;
      }
      adapter.tabs?.sendMessage?.(activeTab.id, { type: "scan_and_capture" }, (response: any) => {
        if (adapter.runtime?.lastError) {
          showToast("MossDL content script is not running on this page");
          return;
        }
        if (response?.capturedCount !== undefined) {
          showToast(`Dispatched ${response.capturedCount} download link(s) to MossDL`);
          if (elements.statCapturedCount) {
            const current = parseInt(elements.statCapturedCount.textContent || "0", 10) || 0;
            elements.statCapturedCount.textContent = String(current + response.capturedCount);
          }
        } else {
          showToast("No downloadable files found on page");
        }
      });
    });
  });

  // Button: Capture media
  elements.btnCaptureMedia?.addEventListener("click", () => {
    adapter.tabs?.query?.({ active: true, currentWindow: true }, (tabs: any[]) => {
      if (adapter.runtime?.lastError || !tabs || tabs.length === 0) {
        showToast("Cannot capture from current tab");
        return;
      }
      const activeTab = tabs[0];
      if (!activeTab || typeof activeTab.id !== "number") {
        showToast("Cannot capture from current tab");
        return;
      }
      adapter.tabs?.sendMessage?.(activeTab.id, { type: "capture_active_media" }, (response: any) => {
        if (adapter.runtime?.lastError) {
          showToast("MossDL is not active on this tab");
          return;
        }
        if (response?.success) {
          showToast("Media stream captured!");
        } else {
          showToast("No active media found on this page");
        }
      });
    });
  });

  // Button: Bring Browser Session (Cookies & Auth)
  elements.btnSyncSession?.addEventListener("click", () => {
    adapter.tabs?.query?.({ active: true, currentWindow: true }, (tabs: any[]) => {
      if (adapter.runtime?.lastError || !tabs || tabs.length === 0) {
        showToast("Cannot access current tab");
        return;
      }
      const activeTab = tabs[0];
      if (!activeTab || !activeTab.url) {
        showToast("No active URL on current tab");
        return;
      }
      try {
        const url = new URL(activeTab.url);
        if (url.protocol !== "http:" && url.protocol !== "https:") {
          showToast("Cannot sync session for internal browser pages");
          return;
        }
        showToast(`Syncing session for ${url.hostname}...`);
        adapter.runtime?.sendMessage?.({
          type: "sync_session",
          page_url: activeTab.url,
          domain: url.hostname,
          store_id: activeTab.cookieStoreId,
        }, (res: any) => {
          if (adapter.runtime?.lastError) {
            showToast("Failed to communicate with background service worker");
            return;
          }
          if (res?.success) {
            showToast(`✓ Synced ${res.count} cookie(s) for ${res.domain} to MossDL!`);
          } else {
            showToast(res?.message || "Could not sync session cookies");
          }
        });
      } catch {
        showToast("Invalid URL on active tab");
      }
    });
  });

  // Button: Open Desktop
  elements.btnOpenDesktop?.addEventListener("click", () => {
    adapter.runtime?.sendMessage?.({ type: "open_desktop" }, () => { void adapter.runtime?.lastError; });
    showToast("Launching MossDL...");
  });
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => {
    void initPopup();
  });
}
