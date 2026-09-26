import { createBrowserAdapter } from "./browserApi";
import { isMediaCandidate } from "./protocol";

const OVERLAY_ATTRIBUTE = "data-transfer-manager-capture";
const DOWNLOAD_EXTENSIONS = new Set([
  ".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2",
  ".iso", ".dmg", ".exe", ".msi", ".apk", ".bin",
  ".mp4", ".mkv", ".webm", ".avi", ".mov",
  ".mp3", ".flac", ".wav", ".aac", ".ogg",
  ".m3u8", ".mpd", ".pdf", ".torrent",
]);

const KNOWN_HOSTER_DOMAINS = new Set([
  "mega.nz", "1fichier.com", "mediafire.com", "rapidgator.net", "pixeldrain.com",
  "buzzheavier.com", "gofile.io", "catbox.moe", "dropbox.com", "drive.google.com",
  "bunkr.is", "bunkr.si", "bunkr.to", "bunkr.ru", "bunkr.cr", "bunkr.black",
  "fuckingfast.co", "datanodes.to", "upload.ee", "mixdrop.co", "streamtape.com",
  "krakenfiles.com",
  "fileditch.com", "iceyfile.com", "rootz.so", "sendvid.com",
  "streamable.com", "vikingfile.com", "voe.sx", "whyp.it",
]);

function isDownloadUrl(rawUrl: string): boolean {
  try {
    const parsed = new URL(rawUrl, location.href);
    if (!["http:", "https:", "ftp:"].includes(parsed.protocol)) return false;
    const pathname = parsed.pathname.toLowerCase();
    if (Array.from(DOWNLOAD_EXTENSIONS).some((ext) => pathname.endsWith(ext))) return true;
    const hostname = parsed.hostname.toLowerCase();
    return Array.from(KNOWN_HOSTER_DOMAINS).some((domain) => hostname === domain || hostname.endsWith("." + domain));
  } catch {
    return false;
  }
}

function createOverlayButton(element: HTMLMediaElement): HTMLElement {
  const button = document.createElement("button");
  button.type = "button";
  button.setAttribute(OVERLAY_ATTRIBUTE, "true");
  button.setAttribute("title", "Capture media with MossDL");
  button.setAttribute("aria-label", "Capture media");
  
  // High-performance styling matching MossDL glassmorphic theme
  Object.assign(button.style, {
    position: "absolute",
    top: "12px",
    right: "12px",
    zIndex: "2147483647",
    display: "inline-flex",
    alignItems: "center",
    gap: "6px",
    padding: "6px 12px",
    borderRadius: "9999px",
    background: "rgba(12, 15, 23, 0.88)",
    backdropFilter: "blur(8px)",
    WebkitBackdropFilter: "blur(8px)",
    border: "1px solid rgba(56, 189, 248, 0.4)",
    color: "#e0f2fe",
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    fontSize: "12px",
    fontWeight: "600",
    cursor: "pointer",
    boxShadow: "0 4px 14px rgba(0, 0, 0, 0.45)",
    transition: "all 0.2s ease",
  });

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", "13");
  svg.setAttribute("height", "13");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("fill", "currentColor");
  svg.style.flexShrink = "0";
  svg.style.color = "#38bdf8";
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("fill-rule", "evenodd");
  path.setAttribute("clip-rule", "evenodd");
  path.setAttribute("d", "M3 17a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm3.293-7.707a1 1 0 011.414 0L9 10.586V3a1 1 0 112 0v7.586l1.293-1.293a1 1 0 111.414 1.414l-3 3a1 1 0 01-1.414 0l-3-3a1 1 0 010-1.414z");
  svg.appendChild(path);

  const labelSpan = document.createElement("span");
  labelSpan.textContent = "Transfer";

  button.appendChild(svg);
  button.appendChild(labelSpan);

  button.addEventListener("mouseenter", () => {
    button.style.transform = "scale(1.04)";
    button.style.borderColor = "rgba(52, 211, 153, 0.8)";
    button.style.boxShadow = "0 0 14px rgba(56, 189, 248, 0.4)";
  });
  button.addEventListener("mouseleave", () => {
    button.style.transform = "scale(1)";
    button.style.borderColor = "rgba(56, 189, 248, 0.4)";
    button.style.boxShadow = "0 4px 14px rgba(0, 0, 0, 0.45)";
  });

  button.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const mediaUrl = element.currentSrc || element.src;
    if (!mediaUrl) return;

    const adapter = createBrowserAdapter();
    adapter.runtime?.sendMessage?.({
      type: "capture_candidate",
      details: {
        url: mediaUrl,
        method: "GET",
        type: "media",
        mediaSelected: true,
        documentUrl: location.href,
        mimeType: element.currentSrc ? "video/*" : "",
      },
    }, () => { void adapter.runtime?.lastError; });

    // Brief visual feedback
    labelSpan.textContent = "✓ Queued";
    labelSpan.style.color = "#34d399";
    setTimeout(() => {
      labelSpan.textContent = "Transfer";
      labelSpan.style.color = "";
    }, 2000);
  });

  return button;
}

function addMediaButton(element: HTMLMediaElement): void {
  const parent = element.parentElement;
  if (!parent || parent.querySelector(`[${OVERLAY_ATTRIBUTE}]`)) return;

  // Ensure parent has relative or absolute positioning so the overlay anchors properly
  const computed = window.getComputedStyle(parent);
  if (computed.position === "static") {
    parent.style.position = "relative";
  }

  parent.appendChild(createOverlayButton(element));
}

export function installMediaOverlay(root: ParentNode = document): void {
  for (const element of Array.from(root.querySelectorAll("video, audio"))) {
    const media = element as HTMLMediaElement;
    if (isMediaCandidate({ type: "media", url: media.currentSrc || media.src })) {
      addMediaButton(media);
    }
  }
}

function scanPageLinks(): string[] {
  const downloadLinks = new Set<string>();
  const externalLinks = new Set<string>();
  const pageHostname = location.hostname.toLowerCase();

  for (const anchor of Array.from(document.querySelectorAll("a[href]"))) {
    const raw = (anchor as HTMLAnchorElement).href;
    if (!raw) continue;
    try {
      const parsed = new URL(raw, location.href);
      if (!["http:", "https:", "ftp:"].includes(parsed.protocol)) continue;
      if (anchor.hasAttribute("download") || isDownloadUrl(raw) || isMediaCandidate({ url: raw })) {
        downloadLinks.add(raw);
      } else if (parsed.hostname.toLowerCase() !== pageHostname) {
        externalLinks.add(raw);
      }
    } catch {
      // Ignore invalid URLs
    }
  }

  if (downloadLinks.size > 0) {
    return Array.from(downloadLinks);
  }
  return Array.from(externalLinks);
}

function handleRuntimeMessages(message: any, _sender: any, sendResponse: (res?: any) => void): boolean {
  if (!message || typeof message.type !== "string") return false;

  if (message.type === "inspect_tab") {
    const mediaList = Array.from(document.querySelectorAll("video, audio"));
    const downloadList = scanPageLinks();
    sendResponse({
      mediaElementsCount: mediaList.length,
      downloadLinksCount: downloadList.length,
    });
    return true;
  }

  if (message.type === "scan_and_capture") {
    const urls = scanPageLinks();
    if (urls.length) {
      const candidates = urls.map((url) => ({
        url,
        method: "GET",
        documentUrl: location.href,
        download: true,
      }));
      const adapter = createBrowserAdapter();
      adapter.runtime?.sendMessage?.({
        type: "capture_batch",
        candidates,
      }, () => { void adapter.runtime?.lastError; });
    }
    sendResponse({ capturedCount: urls.length });
    return true;
  }

  if (message.type === "capture_active_media") {
    const mediaList = Array.from(document.querySelectorAll("video, audio")) as HTMLMediaElement[];
    const active = mediaList.find((m) => !m.paused && m.currentTime > 0) || mediaList[0];
    if (active && (active.currentSrc || active.src)) {
      const adapter = createBrowserAdapter();
      adapter.runtime?.sendMessage?.({
        type: "capture_candidate",
        details: {
          url: active.currentSrc || active.src,
          method: "GET",
          type: "media",
          mediaSelected: true,
          documentUrl: location.href,
        },
      }, () => { void adapter.runtime?.lastError; });
      sendResponse({ success: true, url: active.currentSrc || active.src });
    } else {
      sendResponse({ success: false });
    }
    return true;
  }

  return false;
}

if (typeof document !== "undefined") {
  const adapter = createBrowserAdapter();
  adapter.addListener(adapter.runtime?.onMessage, handleRuntimeMessages);

  // Check overlay preference before installing
  adapter.storage?.local?.get?.("preferences", (data: any) => {
    const prefs = data?.preferences;
    if (prefs?.mediaOverlays !== false) {
      installMediaOverlay();
      new MutationObserver(() => installMediaOverlay()).observe(document.documentElement, {
        childList: true,
        subtree: true,
      });
    }
  });
}
