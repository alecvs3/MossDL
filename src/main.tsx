import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./figma/index.css";
import "./figma/ui/controls.css";
import { emitLog } from "./lib/telemetry";

// ─── Global Verbose Error Interceptors ───────────────────────────────────────
const diagBanner = [
  "============================================================",
  " MossDL Dev Console initialized - Verbose diagnostics active",
  "============================================================",
].join("\n");
console.log(`%c${diagBanner}`, "color: #60a5fa;");

// ─── Suppress Default Browser Shortcuts ─────────────────────────────────────
// Prevent webview defaults that do not belong in a desktop app (e.g. Print, Save Page As, View Source, Caret Browsing)
window.addEventListener(
  "keydown",
  (e: KeyboardEvent) => {
    const ctrlOrMeta = e.ctrlKey || e.metaKey;
    if (ctrlOrMeta) {
      const k = e.key.toLowerCase();
      // Ctrl+P (Print), Ctrl+S (Save As), Ctrl+U (View Source), Ctrl+D (Bookmark),
      // Ctrl+F (Browser Find), Ctrl+G (Find Next), Ctrl+H (Browser History), Ctrl+J (Browser Downloads)
      if (["p", "s", "u", "d", "f", "g", "h", "j"].includes(k)) {
        e.preventDefault();
        return;
      }
      // Suppress accidental reload in production desktop builds
      if (!import.meta.env.DEV && (k === "r" || e.key === "F5")) {
        e.preventDefault();
        return;
      }
    }
    // F3 (Browser Find), F7 (Caret Browsing dialog)
    if (e.key === "F3" || e.key === "F7") {
      e.preventDefault();
      return;
    }
    if (!import.meta.env.DEV && e.key === "F5") {
      e.preventDefault();
      return;
    }
  },
  { capture: true }
);

window.addEventListener("error", (event) => {
  const { error, message, filename, lineno, colno } = event;
  console.error(
    `%c🔥 [UNCAUGHT ERROR] ${message}`,
    "color: #ef4444; font-weight: bold; font-size: 13px;",
  );
  console.error("  File:    ", filename ?? "(unknown)");
  console.error("  Line:Col:", `${lineno}:${colno}`);
  console.error("  Time:    ", new Date().toISOString());
  if (error?.stack) {
    console.error("  Stack:\n", error.stack);
  }
  if (error && typeof error === "object") {
    try {
      const keys = Object.keys(error).filter((k) => k !== "stack" && k !== "message");
      if (keys.length > 0) {
        console.error("  Extra Props:", Object.fromEntries(keys.map((k) => [k, (error as Record<string, unknown>)[k]])));
      }
    } catch { /* ignore */ }
  }
  emitLog("ERROR", "ui:error", String(message || "Uncaught browser exception"), {
    filename,
    lineno,
    colno,
  }, error);
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  const reasonStr = reason instanceof Error
    ? reason.message
    : typeof reason === "string"
      ? reason
      : JSON.stringify(reason);
  console.error(
    `%c🔥 [UNHANDLED PROMISE REJECTION] ${reasonStr}`,
    "color: #f97316; font-weight: bold; font-size: 13px;",
  );
  console.error("  Time:    ", new Date().toISOString());
  if (reason instanceof Error && reason.stack) {
    console.error("  Stack:\n", reason.stack);
  }
  if (reason && typeof reason === "object" && !(reason instanceof Error)) {
    try {
      console.error("  Full Reason:", JSON.stringify(reason, null, 2));
    } catch { /* ignore */ }
  }
  emitLog("ERROR", "ui:unhandled_rejection", `Unhandled Promise Rejection: ${reasonStr}`, {
    reason: typeof reason === "object" ? JSON.stringify(reason) : String(reason),
  }, reason);
});

import { TelemetryErrorBoundary } from "./components/TelemetryErrorBoundary";

// ─── React Mount ─────────────────────────────────────────────────────────────
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TelemetryErrorBoundary>
      <App />
    </TelemetryErrorBoundary>
  </StrictMode>,
);

