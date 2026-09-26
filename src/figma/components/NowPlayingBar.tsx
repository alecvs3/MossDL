import React, { useState, useRef, useEffect } from "react";
import { SiteIcon } from "../ui/SiteIcon";
import { createPortal } from "react-dom";
import type { DownloadWithHistory } from "../types";
import { fmtBytes, pct } from "../types";
import { Icon, ic, detectProvider, providerLabel } from "../icons";
import { shouldRenderNowPlaying } from "../../lib/layout";
import { canPerformTransferAction, normalizeTaskStatus } from "../commands";

export function NowPlayingBar({
  downloads,
  onTaskAction,
  onOpenSheet,
  statusText,
}: {
  downloads: DownloadWithHistory[];
  onTaskAction?: (task: DownloadWithHistory, action: "pause" | "resume" | "stop" | "restart") => void;
  onOpenSheet: (id: string) => void;
  statusText?: string;
}) {
  const [focusId, setFocusId] = useState<string | null>(null);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [progressHover, setProgressHover] = useState(false);
  const [recentlyStopped, setRecentlyStopped] = useState<DownloadWithHistory | null>(null);
  const [menuPosition, setMenuPosition] = useState<{ left: number; bottom: number } | null>(null);
  const titleRef = useRef<HTMLDivElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const timeoutRef = useRef<number | null>(null);

  const active = downloads.filter((d) => ["downloading", "paused", "queued"].includes(d.status));
  const defaultFocused =
    [...active].reverse().find((d) => d.status === "downloading") ??
    active[active.length - 1] ??
    null;
  const focused =
    recentlyStopped ??
    (focusId ? active.find((d) => d.id === focusId) : null) ??
    defaultFocused;

  useEffect(() => {
    if (focusId && !active.find((d) => d.id === focusId) && !recentlyStopped) {
      setFocusId(null);
    }
  }, [active, focusId, recentlyStopped]);

  useEffect(
    () => () => {
      if (timeoutRef.current) window.clearTimeout(timeoutRef.current);
    },
    []
  );

  useEffect(() => {
    if (!dropdownOpen) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        titleRef.current &&
        !titleRef.current.contains(target) &&
        dropdownRef.current &&
        !dropdownRef.current.contains(target)
      ) {
        setDropdownOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDropdownOpen(false);
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("keydown", onKey);
    };
  }, [dropdownOpen]);

  if (!focused || !shouldRenderNowPlaying(focused.status, Boolean(recentlyStopped))) return null;

  const p = pct(focused);
  const provider = detectProvider(focused.url, focused.provider);
  const canExpand = active.length > 1;
  const commandStatus = normalizeTaskStatus(focused.status);
  const canPause = canPerformTransferAction(commandStatus, "pause");
  const canResume = canPerformTransferAction(commandStatus, "resume");
  const showRestart = recentlyStopped?.id === focused.id && canPerformTransferAction(commandStatus, "restart");

  const toggleFocused = () => {
    const action = canPause ? "pause" : canResume ? "resume" : null;
    if (action) onTaskAction?.(focused, action);
  };
  const stopFocused = () => {
    // Track only that a stop was requested for this task; the rendered status
    // comes from engine state, never a locally fabricated "cancelled".
    setRecentlyStopped({ ...focused });
    if (timeoutRef.current) window.clearTimeout(timeoutRef.current);
    timeoutRef.current = window.setTimeout(() => setRecentlyStopped(null), 8000);
    onTaskAction?.(focused, "stop");
  };
  const runRestart = () => {
    setRecentlyStopped(null);
    onTaskAction?.(focused, "restart");
  };
  const openFromBackground = (event: React.MouseEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (target.closest("button, a, input, select, textarea, [role='button'], [data-interactive-control='true']"))
      return;
    onOpenSheet(focused.id);
  };
  const toggleDropdown = () => {
    if (!titleRef.current || !canExpand) return;
    const rect = titleRef.current.getBoundingClientRect();
    setMenuPosition({ left: rect.left, bottom: window.innerHeight - rect.top + 8 });
    setDropdownOpen((open) => !open);
  };

  return (
    <div className="now-playing-bar" onClick={openFromBackground}>
      {statusText && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "8px",
            padding: "0 14px",
            height: "100%",
            color: "#60a5fa",
            fontSize: "11px",
            fontWeight: 600,
            animation: "pulse 2s ease-in-out infinite",
          }}
        >
          <span style={{ fontSize: "13px" }}>⟳</span>
          {statusText}
        </div>
      )}
      {!statusText && (
      <>
      {/* title pill — doubles as dropdown trigger when multiple active */}
      <div ref={titleRef} className="now-playing-title" style={{ position: "relative" }}>
        <div
          onClick={toggleDropdown}
          data-interactive-control="true"
          style={{
            display: "flex",
            alignItems: "center",
            gap: "8px",
            padding: "4px 8px 4px 6px",
            borderRadius: "7px",
            cursor: canExpand ? "pointer" : "default",
            transition: "background 0.12s",
            width: "100%",
          }}
          onMouseEnter={(e) => {
            if (canExpand) e.currentTarget.style.background = "var(--surface-08)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = "transparent";
          }}
        >
          <SiteIcon url={focused.url} size={14} />
          <div className="min-w-0">
            <p
              className="truncate"
              style={{
                fontSize: "10.5px",
                fontWeight: 700,
                color: "var(--ink-90)",
                lineHeight: 1,
                maxWidth: "160px",
              }}
            >
              {focused.name}
            </p>
            <p style={{ fontSize: "8px", color: "var(--ink-30)", marginTop: "2px" }}>
              {providerLabel(provider)}
            </p>
          </div>
          {canExpand && (
            <Icon
              d={ic.chevronDown}
              size={9}
              style={{
                flexShrink: 0,
                color: "var(--ink-40)",
                transform: dropdownOpen ? "rotate(180deg)" : "rotate(0deg)",
                transition: "transform 0.18s",
              }}
            />
          )}
        </div>

        {/* dropdown — opens upward above the bar */}
        {dropdownOpen &&
          createPortal(
            <div
              ref={dropdownRef}
              onMouseDown={(e) => e.stopPropagation()}
              style={{
                position: "fixed",
                bottom: menuPosition?.bottom ?? 60,
                left: menuPosition?.left ?? 8,
                background: "rgba(14,14,22,0.98)",
                backdropFilter: "blur(24px) saturate(160%)",
                border: "1px solid var(--line-10)",
                borderRadius: "10px",
                padding: "5px",
                minWidth: "300px",
                maxWidth: "400px",
                zIndex: 60,
                boxShadow: "0 12px 40px rgba(0,0,0,0.7)",
              }}
              className="now-playing-dropdown"
            >
              {active.map((dl) => {
                const dlP = pct(dl);
                const dlProv = detectProvider(dl.url, dl.provider);
                const isFocused = dl.id === focused.id;
                return (
                  <button
                    key={dl.id}
                    onClick={() => {
                      setRecentlyStopped(null);
                      setFocusId(dl.id);
                      setDropdownOpen(false);
                    }}
                    style={{
                      display: "flex",
                      alignItems: "flex-start",
                      gap: "9px",
                      width: "100%",
                      padding: "8px 10px",
                      borderRadius: "6px",
                      border: "none",
                      cursor: "pointer",
                      background: isFocused ? "rgba(0,120,212,0.18)" : "transparent",
                      color: "var(--ink-85)",
                      textAlign: "left",
                      transition: "background 0.1s",
                    }}
                    onMouseEnter={(e) => {
                      if (!isFocused) e.currentTarget.style.background = "var(--surface-06)";
                    }}
                    onMouseLeave={(e) => {
                      if (!isFocused) e.currentTarget.style.background = "transparent";
                    }}
                  >
                    <span style={{ marginTop: "1px", flexShrink: 0, display: "flex" }}>
                      <SiteIcon url={dl.url} size={11} />
                    </span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: "8px",
                          marginBottom: "5px",
                        }}
                      >
                        <span
                          className="truncate"
                          style={{ fontSize: "9.5px", fontWeight: 600, color: "var(--ink-85)" }}
                        >
                          {dl.name}
                        </span>
                        <span
                          style={{
                            fontSize: "8.5px",
                            color: "var(--ink-45)",
                            fontFamily: "'JetBrains Mono',monospace",
                            flexShrink: 0,
                          }}
                        >
                          {dlP}%
                        </span>
                      </div>
                      <div
                        style={{
                          height: "2px",
                          borderRadius: "1px",
                          background: "var(--surface-10)",
                          overflow: "hidden",
                          marginBottom: "4px",
                        }}
                      >
                        <div
                          style={{
                            height: "100%",
                            width: `${dlP}%`,
                            background: isFocused ? "var(--accent-light)" : "rgba(77,166,245,0.6)",
                            borderRadius: "1px",
                            transition: "width 0.4s linear",
                          }}
                        />
                      </div>
                      <div style={{ display: "flex", gap: "8px" }}>
                        <span
                          style={{
                            fontSize: "8px",
                            color: "var(--accent-light)",
                            fontFamily: "'JetBrains Mono',monospace",
                          }}
                        >
                          {fmtBytes(dl.speed)}/s
                        </span>
                        <span
                          style={{
                            fontSize: "8px",
                            color: "var(--ink-30)",
                            fontFamily: "'JetBrains Mono',monospace",
                          }}
                        >
                          {dl.eta}
                        </span>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>,
            document.body
          )}
      </div>

      {/* progress bar + stats */}
      <div
        className="now-playing-progress"
        style={{ display: "flex", alignItems: "center", gap: "8px", position: "relative" }}
        onMouseEnter={() => setProgressHover(true)}
        onMouseLeave={() => setProgressHover(false)}
      >
        <div
          style={{
            flex: 1,
            height: "3px",
            borderRadius: "2px",
            background: "var(--surface-10)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              height: "100%",
              width: `${p}%`,
              background: "var(--accent-light)",
              borderRadius: "2px",
              transition: "width 0.4s linear",
            }}
          />
        </div>
        <span
          style={{
            fontSize: "9px",
            color: "var(--ink-40)",
            fontFamily: "'JetBrains Mono',monospace",
            flexShrink: 0,
          }}
        >
          {p}%
        </span>
        <span
          className="now-playing-speed"
          style={{ fontSize: "9px", color: "var(--accent-light)", fontFamily: "'JetBrains Mono',monospace", flexShrink: 0 }}
        >
          {fmtBytes(focused.speed)}/s
        </span>
        <span
          className="now-playing-eta"
          style={{ fontSize: "9px", color: "var(--ink-30)", fontFamily: "'JetBrains Mono',monospace", flexShrink: 0 }}
        >
          {focused.eta}
        </span>
        {/* hover overlay with controls */}
        {progressHover && !showRestart && (
          <div
            style={{
              position: "absolute",
              inset: "-6px -4px",
              display: "flex",
              alignItems: "center",
              justifyContent: "flex-start",
              gap: "6px",
              background: "rgba(10,10,18,0.72)",
              backdropFilter: "blur(10px) saturate(140%)",
              borderRadius: "6px",
            }}
          >
            <button
              onClick={(e) => {
                e.stopPropagation();
                toggleFocused();
              }}
              className={`now-playing-hover-btn ${canResume ? "resume-control" : "pause-control"}`}
              style={{
                width: 28,
                height: 28,
                borderRadius: "50%",
                border: "none",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                transition: "background 0.12s, color 0.12s",
              }}
              title={canPause ? "Pause" : "Resume"}
            >
              <Icon d={canPause ? ic.pauseAll : ic.playAll} size={11} />
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation();
                stopFocused();
              }}
              style={{
                width: 28,
                height: 28,
                borderRadius: "50%",
                border: "none",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                background: "rgba(220,60,60,0.12)",
                color: "rgba(220,80,80,0.7)",
                transition: "background 0.12s, color 0.12s",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = "rgba(220,60,60,0.28)";
                e.currentTarget.style.color = "rgba(248,100,100,0.95)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "rgba(220,60,60,0.12)";
                e.currentTarget.style.color = "rgba(220,80,80,0.7)";
              }}
              title="Stop"
            >
              <Icon d={ic.stopAll} size={11} />
            </button>
          </div>
        )}
      </div>

      {/* controls */}
      <div className="now-playing-actions" style={{ display: "flex", alignItems: "center", gap: "4px" }}>
        {showRestart ? (
          <button onClick={runRestart} className="now-playing-btn restart-control" title="Restart download">
            <Icon d={ic.refreshCw} size={11} />
            <span>Restart</span>
          </button>
        ) : (
          <>
            <button
              onClick={toggleFocused}
              className={`now-playing-btn ${canResume ? "resume-control" : "pause-control"}`}
              title={canPause ? "Pause" : "Resume"}
            >
              <Icon d={canPause ? ic.pauseAll : ic.playAll} size={11} />
            </button>
            <button onClick={stopFocused} className="now-playing-btn stop-control" title="Stop">
              <Icon d={ic.stopAll} size={11} />
            </button>
          </>
        )}
        <button
          onClick={() => onOpenSheet(focused.id)}
          className="now-playing-btn"
          title="View details"
          style={{
            fontSize: "13px",
            fontWeight: 500,
            letterSpacing: "-0.5px",
            color: "var(--ink-35)",
          }}
        >
          ···
        </button>
      </div>
      </>
      )}
    </div>
  );
}
