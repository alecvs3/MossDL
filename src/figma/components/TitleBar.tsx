import React, { useState, useEffect } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { windowAction, logsQuery } from "../../api";
import { Icon, ic } from "../icons";
import logoUrl from "../assets/brand/mossdl-logo.svg";

// Diagnostics counter: a development affordance, not something a shipped app
// puts in its title bar. Dev Logs stay reachable from View → Dev Logs Console.
const SHOW_DIAGNOSTIC_BADGE = import.meta.env.DEV;

export function TitleBar({
  systemTray = false,
  onToggleDevLogs,
  isDevLogsOpen = false,
  children,
}: {
  systemTray?: boolean;
  onToggleDevLogs?: (initialLevel?: "ERROR" | "ALL") => void;
  isDevLogsOpen?: boolean;
  /** Menus rendered inside the title bar, the way Notepad and Terminal do. */
  children?: React.ReactNode;
}) {
  const [appWindow] = useState(() => {
    try {
      return typeof window !== "undefined" && (window as any).__TAURI_INTERNALS__ ? getCurrentWindow() : null;
    } catch {
      return null;
    }
  });
  const [maximized, setMaximized] = useState(false);
  const [errorCount, setErrorCount] = useState(0);
  const [warnCount, setWarnCount] = useState(0);
  const [hasNewIssue, setHasNewIssue] = useState(false);

  // Poll for diagnostic counts
  useEffect(() => {
    if (!SHOW_DIAGNOSTIC_BADGE) return;
    let lastErrors = 0;
    const updateCounts = async () => {
      try {
        const res = await logsQuery({ limit: 500 });
        if (res && res.events) {
          let errs = 0;
          let warns = 0;
          for (const ev of res.events) {
            const lvl = ev.level.toUpperCase();
            if (lvl === "ERROR" || lvl === "CRITICAL") errs++;
            else if (lvl === "WARN" || lvl === "WARNING") warns++;
          }
          if (errs > lastErrors && lastErrors >= 0) {
            setHasNewIssue(true);
            setTimeout(() => setHasNewIssue(false), 3000);
          }
          lastErrors = errs;
          setErrorCount(errs);
          setWarnCount(warns);
        }
      } catch {
        // Ignore
      }
    };
    void updateCounts();
    const interval = window.setInterval(() => void updateCounts(), 2000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    if (!appWindow) return;
    let unlisten: (() => void) | undefined;
    let debounceTimer: number | undefined;
    const sync = () => {
      if (debounceTimer) window.clearTimeout(debounceTimer);
      debounceTimer = window.setTimeout(() => {
        void appWindow.isMaximized().then(setMaximized).catch(() => {});
      }, 100);
    };
    void appWindow.isMaximized().then(setMaximized).catch(() => {});
    void appWindow.onResized(sync).then((dispose) => {
      unlisten = dispose;
    }).catch(() => {});
    return () => {
      if (debounceTimer) window.clearTimeout(debounceTimer);
      unlisten?.();
    };
  }, [appWindow]);

  useEffect(() => {
    if (!appWindow) return;
    let unlisten: (() => void) | undefined;
    void appWindow.onCloseRequested(async (event) => {
      if (!systemTray) return;
      event.preventDefault();
      await appWindow.hide().catch(() => {});
    }).then((dispose) => {
      unlisten = dispose;
    }).catch(() => {});
    return () => {
      unlisten?.();
    };
  }, [appWindow, systemTray]);

  const actions: ("minimize" | "toggle_maximize" | "close")[] = ["minimize", "toggle_maximize", "close"];
  const startDrag = (event: React.MouseEvent<HTMLDivElement>) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest("button, input, select, textarea, [data-interactive='true']")) return;
    void windowAction("start_dragging").catch((error) =>
      console.error("Could not drag the application window", error)
    );
  };

  return (
    <div
      className="app-titlebar flex items-center justify-between h-9 pl-3 pr-0 select-none shrink-0"
      data-tauri-drag-region="false"
      onMouseDown={startDrag}
      onDoubleClick={(event) => {
        if (!(event.target as HTMLElement).closest("button, input, select, textarea, [data-interactive='true']")) {
          void windowAction("toggle_maximize").catch((error) =>
            console.error("Could not toggle the application window", error)
          );
        }
      }}
      style={{ background: "var(--titlebar-bg)", borderBottom: "1px solid var(--border-subtle)" }}
    >
      <div className="flex items-center gap-2 min-w-0">
        <img src={logoUrl} alt="" width={16} height={16} draggable={false} className="shrink-0" style={{ borderRadius: 4 }} />
        <span
          className="shrink-0"
          style={{
            fontSize: "11.5px",
            fontWeight: 700,
            color: "var(--text-secondary)",
            letterSpacing: "0.02em",
          }}
        >
          MossDL
        </span>
        {children}
      </div>

      <div className="flex items-center">
        {/* Diagnostic Error & Warning Badge (development builds only) */}
        {SHOW_DIAGNOSTIC_BADGE && onToggleDevLogs && (
          <button
            data-interactive="true"
            data-tauri-drag-region="false"
            onClick={() => onToggleDevLogs(errorCount > 0 ? "ERROR" : "ALL")}
            title={
              errorCount > 0
                ? `${errorCount} error${errorCount > 1 ? "s" : ""}, ${warnCount} warning${warnCount > 1 ? "s" : ""} recorded — Click to open Dev Logs`
                : warnCount > 0
                ? `${warnCount} warning${warnCount > 1 ? "s" : ""} recorded — Click to open Dev Logs`
                : "Diagnostics: Clean — Click to open Dev Logs"
            }
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              padding: "2px 8px",
              marginRight: "8px",
              borderRadius: "4px",
              fontSize: "10px",
              fontWeight: 600,
              fontFamily: "'JetBrains Mono', Consolas, monospace",
              cursor: "pointer",
              border: errorCount > 0
                ? "1px solid rgba(239, 68, 68, 0.4)"
                : warnCount > 0
                ? "1px solid rgba(245, 158, 11, 0.4)"
                : "1px solid rgba(255, 255, 255, 0.1)",
              backgroundColor: hasNewIssue
                ? "rgba(239, 68, 68, 0.35)"
                : isDevLogsOpen
                ? "rgba(59, 130, 246, 0.2)"
                : errorCount > 0
                ? "rgba(239, 68, 68, 0.15)"
                : warnCount > 0
                ? "rgba(245, 158, 11, 0.12)"
                : "rgba(255, 255, 255, 0.04)",
              color: errorCount > 0
                ? "var(--danger)"
                : warnCount > 0
                ? "var(--warning)"
                : "var(--text-muted)",
              boxShadow: hasNewIssue ? "0 0 10px rgba(239, 68, 68, 0.5)" : "none",
              transition: "all 0.18s ease",
            }}
          >
            {errorCount > 0 ? (
              <>
                <span style={{ fontSize: "9px" }}>🔴</span>
                <span>{errorCount}</span>
                {warnCount > 0 && (
                  <>
                    <span style={{ opacity: 0.4 }}>|</span>
                    <span style={{ fontSize: "9px" }}>⚠️</span>
                    <span>{warnCount}</span>
                  </>
                )}
              </>
            ) : warnCount > 0 ? (
              <>
                <span style={{ fontSize: "9px" }}>⚠️</span>
                <span>{warnCount}</span>
              </>
            ) : (
              <>
                <span style={{ fontSize: "8px", color: "var(--success)" }}>●</span>
                <span style={{ opacity: 0.8 }}>Logs</span>
              </>
            )}
          </button>
        )}

        {/* Window control buttons */}
        {[
          { d: ic.minimize, hov: "var(--control-hover)" },
          { d: maximized ? ic.restore : ic.maximize, hov: "var(--control-hover)" },
          { d: ic.close, hov: "var(--close-hover)" },
        ].map((b, i) => (
          <button
            key={i}
            className={`window-control flex items-center justify-center w-11 h-8 transition-colors${
              i === 2 ? " close" : ""
            }`}
            onClick={() =>
              void windowAction(actions[i])
                .then(() => {
                  if (i === 1)
                    setTimeout(() => void appWindow?.isMaximized().then(setMaximized).catch(() => {}), 0);
                })
                .catch((error) => console.error("Could not apply window action", error))
            }
            aria-label={i === 0 ? "Minimize" : i === 1 ? (maximized ? "Restore" : "Maximize") : "Close"}
            title={i === 0 ? "Minimize" : i === 1 ? (maximized ? "Restore" : "Maximize") : "Close"}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = b.hov;
              e.currentTarget.style.color = "var(--text-primary)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "transparent";
              e.currentTarget.style.color = "var(--text-muted)";
            }}
          >
            <Icon d={b.d} size={11} />
          </button>
        ))}
      </div>
    </div>
  );
}
