import React, { useState, useEffect, useMemo, useRef, useCallback } from "react";
import {
  logsQuery,
  logsClear,
  openDevLogsWindow,
  type LogEvent,
  type LogLevel,
} from "../api";

export interface DevLogConsoleProps {
  standalone?: boolean;
  onPopOut?: () => void;
  onClose?: () => void;
  initialLevel?: LogLevel | "ALL";
}

const LEVEL_COLORS: Record<string, { bg: string; text: string; border: string }> = {
  CRITICAL: { bg: "rgba(239, 68, 68, 0.25)", text: "#fca5a5", border: "rgba(239, 68, 68, 0.5)" },
  ERROR: { bg: "rgba(239, 68, 68, 0.2)", text: "var(--danger)", border: "rgba(239, 68, 68, 0.4)" },
  WARNING: { bg: "rgba(245, 158, 11, 0.2)", text: "#fbbf24", border: "rgba(245, 158, 11, 0.4)" },
  WARN: { bg: "rgba(245, 158, 11, 0.2)", text: "#fbbf24", border: "rgba(245, 158, 11, 0.4)" },
  INFO: { bg: "rgba(59, 130, 246, 0.2)", text: "#60a5fa", border: "rgba(59, 130, 246, 0.4)" },
  DEBUG: { bg: "rgba(148, 163, 184, 0.15)", text: "#94a3b8", border: "rgba(148, 163, 184, 0.3)" },
};

function formatTimestamp(isoStr: string): string {
  try {
    const d = new Date(isoStr);
    const h = String(d.getHours()).padStart(2, "0");
    const m = String(d.getMinutes()).padStart(2, "0");
    const s = String(d.getSeconds()).padStart(2, "0");
    const ms = String(d.getMilliseconds()).padStart(3, "0");
    return `${h}:${m}:${s}.${ms}`;
  } catch {
    return isoStr;
  }
}

export type ProcessCategory =
  | "ALL"
  | "DOWNLOADS_LIFECYCLE"
  | "TRANSFERS_STALLS"
  | "DOM_CRAWLER"
  | "SHORTLINKS_TIMERS"
  | "BROWSER_CAPTCHA"
  | "VPN_ROUTES";

const PROCESS_CATEGORIES: { id: ProcessCategory; label: string; icon: string; subsystems: string[] }[] = [
  { id: "ALL", label: "All Events", icon: "📋", subsystems: [] },
  {
    id: "DOWNLOADS_LIFECYCLE",
    label: "Downloads & Lifecycle",
    icon: "📥",
    subsystems: [
      "engine:download",
      "engine:transfer",
      "engine:lifecycle",
      "engine:resolve",
      "engine:resolver",
      "engine:task",
      "engine:scheduler",
      "engine:captcha",
      "engine:limits",
      "provider:",
      "engine:browser_solver",
      "engine:concurrency",
    ],
  },
  { id: "TRANSFERS_STALLS", label: "Transfers & Stalls", icon: "⚡", subsystems: ["engine:transfer", "engine:download", "engine:scheduler", "engine:limits", "engine:storage", "engine:concurrency"] },
  { id: "DOM_CRAWLER", label: "DOM & Crawler", icon: "🌐", subsystems: ["engine:crawler", "engine:dom_cleaner", "engine:matrix", "engine:grabber"] },
  { id: "SHORTLINKS_TIMERS", label: "Shortlinks & Timers", icon: "⏳", subsystems: ["provider:shortlink", "engine:shortlink", "engine:bypass", "engine:resolver"] },
  { id: "BROWSER_CAPTCHA", label: "Browser & Captchas", icon: "🤖", subsystems: ["engine:captcha", "engine:flaresolverr", "engine:playwright", "browser:capture", "engine:browser_solver"] },
  { id: "VPN_ROUTES", label: "VPN & Routes", icon: "🛡️", subsystems: ["engine:routes", "engine:proxy", "engine:socks5", "engine:vpn"] },
];

export function DevLogConsole({
  standalone = false,
  onPopOut,
  onClose,
  initialLevel = "ALL",
}: DevLogConsoleProps) {
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [selectedLevel, setSelectedLevel] = useState<string>(initialLevel);
  const [selectedProcessCategory, setSelectedProcessCategory] = useState<ProcessCategory>("ALL");
  const [selectedSubsystem, setSelectedSubsystem] = useState<string>("ALL");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [autoScroll, setAutoScroll] = useState<boolean>(true);
  const [expandedRowId, setExpandedRowId] = useState<number | null>(null);
  const [copiedNotification, setCopiedNotification] = useState<string | null>(null);

  const lastSeqIdRef = useRef<number>(0);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const userScrolledUpRef = useRef<boolean>(false);

  useEffect(() => {
    if (initialLevel) {
      setSelectedLevel(initialLevel);
    }
  }, [initialLevel]);

  // Periodic polling
  const fetchLogs = useCallback(async () => {
    try {
      const res = await logsQuery({
        since_id: lastSeqIdRef.current > 0 ? lastSeqIdRef.current : undefined,
        limit: 250,
      });
      if (res && res.events && res.events.length > 0) {
        setEvents((prev) => {
          const existingIds = new Set(prev.map((e) => e.id));
          const newEntries = res.events.filter((e) => !existingIds.has(e.id));
          if (newEntries.length === 0) return prev;
          const combined = [...prev, ...newEntries];
          return combined.slice(-5000); // retain latest 5,000 in frontend
        });

        const maxId = Math.max(...res.events.map((e) => e.id));
        if (maxId > lastSeqIdRef.current) {
          lastSeqIdRef.current = maxId;
        }
      }
    } catch {
      // Ignore background query errors
    }
  }, []);

  useEffect(() => {
    void fetchLogs();
    const interval = window.setInterval(() => void fetchLogs(), 1000);
    return () => window.clearInterval(interval);
  }, [fetchLogs]);

  // Handle auto-scroll
  useEffect(() => {
    if (autoScroll && !userScrolledUpRef.current && scrollContainerRef.current) {
      scrollContainerRef.current.scrollTop = scrollContainerRef.current.scrollHeight;
    }
  }, [events, autoScroll]);

  const handleScroll = () => {
    if (!scrollContainerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollContainerRef.current;
    const atBottom = scrollHeight - (scrollTop + clientHeight) < 40;
    userScrolledUpRef.current = !atBottom;
  };

  // Derive counts and available subsystems
  const { counts, availableSubsystems } = useMemo(() => {
    const c = { ALL: events.length, ERROR: 0, WARN: 0, INFO: 0, DEBUG: 0 };
    const subs = new Set<string>();

    for (const ev of events) {
      const lvl = ev.level.toUpperCase();
      if (lvl === "ERROR" || lvl === "CRITICAL") c.ERROR++;
      else if (lvl === "WARN" || lvl === "WARNING") c.WARN++;
      else if (lvl === "INFO") c.INFO++;
      else if (lvl === "DEBUG") c.DEBUG++;

      if (ev.subsystem) {
        subs.add(ev.subsystem);
      }
    }

    return {
      counts: c,
      availableSubsystems: Array.from(subs).sort(),
    };
  }, [events]);

  // Filter events
  const filteredEvents = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    let regex: RegExp | null = null;
    try {
      if (query.startsWith("/") && query.endsWith("/") && query.length > 2) {
        regex = new RegExp(query.slice(1, -1), "i");
      }
    } catch {
      regex = null;
    }

    return events.filter((ev) => {
      // Level check
      if (selectedLevel !== "ALL") {
        const lvl = ev.level.toUpperCase();
        if (selectedLevel === "ERROR" && lvl !== "ERROR" && lvl !== "CRITICAL") return false;
        if (selectedLevel === "WARN" && lvl !== "WARN" && lvl !== "WARNING") return false;
        if (selectedLevel === "INFO" && lvl !== "INFO") return false;
        if (selectedLevel === "DEBUG" && lvl !== "DEBUG") return false;
      }

      // Process Category check
      if (selectedProcessCategory !== "ALL") {
        const cat = PROCESS_CATEGORIES.find((c) => c.id === selectedProcessCategory);
        if (cat && cat.subsystems.length > 0) {
          const match = cat.subsystems.some((sub) =>
            ev.subsystem.toLowerCase().startsWith(sub.toLowerCase())
          );
          if (!match) return false;
        }
      }

      // Subsystem check
      if (selectedSubsystem !== "ALL" && ev.subsystem !== selectedSubsystem) {
        return false;
      }

      // Query check
      if (query) {
        if (regex) {
          return (
            regex.test(ev.message) ||
            regex.test(ev.subsystem) ||
            (ev.error && regex.test(JSON.stringify(ev.error))) ||
            (ev.context && regex.test(JSON.stringify(ev.context)))
          );
        }
        return (
          ev.message.toLowerCase().includes(query) ||
          ev.subsystem.toLowerCase().includes(query) ||
          (ev.error && JSON.stringify(ev.error).toLowerCase().includes(query)) ||
          (ev.context && JSON.stringify(ev.context).toLowerCase().includes(query))
        );
      }

      return true;
    });
  }, [events, selectedLevel, selectedProcessCategory, selectedSubsystem, searchQuery]);

  const handleClear = async () => {
    try {
      await logsClear();
      setEvents([]);
      lastSeqIdRef.current = 0;
      setExpandedRowId(null);
    } catch {
      // Ignore
    }
  };

  const handleCopyReport = async () => {
    try {
      const errorLogs = events.filter((e) => ["ERROR", "CRITICAL"].includes(e.level.toUpperCase()));
      const warnLogs = events.filter((e) => ["WARN", "WARNING"].includes(e.level.toUpperCase()));

      const lines = [
        "# MossDL Diagnostics Report",
        `Generated: ${new Date().toISOString()}`,
        `Total Log Events: ${events.length}`,
        `Errors: ${errorLogs.length} | Warnings: ${warnLogs.length}`,
        "",
        "## Recent Errors & Exceptions",
      ];

      if (errorLogs.length === 0) {
        lines.push("No errors logged.");
      } else {
        errorLogs.slice(-15).forEach((e, idx) => {
          lines.push(`### [${idx + 1}] [${e.level}] ${e.subsystem} (${e.timestamp})`);
          lines.push(`Message: ${e.message}`);
          if (e.duration_ms) lines.push(`Duration: ${e.duration_ms}ms`);
          if (e.context) {
            lines.push("```json");
            lines.push(JSON.stringify(e.context, null, 2));
            lines.push("```");
          }
          if (e.error) {
            lines.push("```");
            lines.push(`${e.error.type}: ${e.error.message}`);
            if (e.error.traceback) lines.push(e.error.traceback);
            lines.push("```");
          }
          lines.push("");
        });
      }

      await navigator.clipboard.writeText(lines.join("\n"));
      setCopiedNotification("Diagnostic report copied to clipboard!");
      setTimeout(() => setCopiedNotification(null), 2500);
    } catch (err) {
      console.error("Could not copy report:", err);
    }
  };

  const handleCopyFiltered = async () => {
    try {
      if (filteredEvents.length === 0) {
        setCopiedNotification("No logs in current filter to copy");
        setTimeout(() => setCopiedNotification(null), 2000);
        return;
      }
      const lines = filteredEvents.map((e) => {
        let line = `[${formatTimestamp(e.timestamp)}] [${e.level}] [${e.subsystem}] ${e.message}`;
        if (e.duration_ms) line += ` (${e.duration_ms}ms)`;
        if (e.error) line += ` | ERROR: ${e.error.type}: ${e.error.message}`;
        if (e.context && Object.keys(e.context).length > 0) line += ` | CONTEXT: ${JSON.stringify(e.context)}`;
        return line;
      });
      await navigator.clipboard.writeText(lines.join("\n"));
      setCopiedNotification(`Copied ${filteredEvents.length} log lines!`);
      setTimeout(() => setCopiedNotification(null), 2500);
    } catch (err) {
      console.error("Could not copy filtered logs:", err);
    }
  };

  const copyText = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedNotification(`${label} copied!`);
      setTimeout(() => setCopiedNotification(null), 2000);
    } catch (err) {
      console.error("Could not copy text:", err);
    }
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        width: "100%",
        backgroundColor: "#090d16",
        color: "#e2e8f0",
        fontFamily: "'JetBrains Mono', Consolas, Monaco, monospace",
        fontSize: "12px",
        overflow: "hidden",
        borderTop: standalone ? "none" : "1px solid rgba(255,255,255,0.1)",
        boxSizing: "border-box",
      }}
    >
      {/* Toolbar Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "8px 12px",
          backgroundColor: "#0d1322",
          borderBottom: "1px solid var(--line-07)",
          gap: "10px",
          flexWrap: "wrap",
          flexShrink: 0,
        }}
      >
        {/* Left: Filter Pills */}
        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          {(["ALL", "ERROR", "WARN", "INFO", "DEBUG"] as const).map((lvl) => {
            const isSelected = selectedLevel === lvl;
            const count = counts[lvl as keyof typeof counts] ?? 0;
            return (
              <button
                key={lvl}
                onClick={() => setSelectedLevel(lvl)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "5px",
                  padding: "4px 8px",
                  borderRadius: "4px",
                  fontSize: "11px",
                  fontWeight: 600,
                  border: isSelected ? "1px solid rgba(59,130,246,0.6)" : "1px solid rgba(255,255,255,0.1)",
                  backgroundColor: isSelected ? "rgba(59,130,246,0.2)" : "rgba(255,255,255,0.03)",
                  color: isSelected ? "#93c5fd" : "#94a3b8",
                  cursor: "pointer",
                  transition: "all 0.1s ease",
                }}
              >
                <span>{lvl}</span>
                <span
                  style={{
                    fontSize: "10px",
                    padding: "1px 4px",
                    borderRadius: "3px",
                    backgroundColor: "rgba(0,0,0,0.3)",
                    color: lvl === "ERROR" && count > 0 ? "var(--danger)" : "inherit",
                  }}
                >
                  {count}
                </span>
              </button>
            );
          })}

          {/* Subsystem Dropdown */}
          <select
            value={selectedSubsystem}
            onChange={(e) => setSelectedSubsystem(e.target.value)}
            style={{
              padding: "4px 8px",
              borderRadius: "4px",
              fontSize: "11px",
              backgroundColor: "var(--surface-05)",
              color: "#cbd5e1",
              border: "1px solid var(--line-10)",
              outline: "none",
              cursor: "pointer",
            }}
          >
            <option value="ALL">All Subsystems</option>
            {availableSubsystems.map((sub) => (
              <option key={sub} value={sub}>
                {sub}
              </option>
            ))}
          </select>
        </div>

        {/* Center: Search Query */}
        <div style={{ flex: 1, minWidth: "160px", maxWidth: "340px", position: "relative" }}>
          <input
            type="text"
            placeholder="Search logs (/regex/ supported)..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              width: "100%",
              padding: "4px 8px",
              paddingRight: searchQuery ? "24px" : "8px",
              borderRadius: "4px",
              fontSize: "11px",
              backgroundColor: "var(--surface-05)",
              color: "#f8fafc",
              border: "1px solid var(--line-10)",
              outline: "none",
              boxSizing: "border-box",
            }}
          />
          {searchQuery && (
            <button
              onClick={() => setSearchQuery("")}
              style={{
                position: "absolute",
                right: "6px",
                top: "50%",
                transform: "translateY(-50%)",
                background: "none",
                border: "none",
                color: "#94a3b8",
                cursor: "pointer",
                padding: 0,
                fontSize: "12px",
              }}
            >
              ✕
            </button>
          )}
        </div>

        {/* Right: Actions */}
        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <button
            onClick={() => setAutoScroll((v) => !v)}
            title="Toggle sticky scroll to bottom"
            style={{
              padding: "4px 8px",
              borderRadius: "4px",
              fontSize: "11px",
              border: autoScroll ? "1px solid rgba(16,185,129,0.5)" : "1px solid rgba(255,255,255,0.1)",
              backgroundColor: autoScroll ? "rgba(16,185,129,0.15)" : "rgba(255,255,255,0.03)",
              color: autoScroll ? "#6ee7b7" : "#94a3b8",
              cursor: "pointer",
            }}
          >
            Auto-scroll: {autoScroll ? "ON" : "OFF"}
          </button>

          <button
            onClick={handleCopyFiltered}
            title="Copy currently filtered logs to clipboard"
            style={{
              padding: "4px 8px",
              borderRadius: "4px",
              fontSize: "11px",
              border: "1px solid rgba(59,130,246,0.4)",
              backgroundColor: "rgba(59,130,246,0.1)",
              color: "#93c5fd",
              cursor: "pointer",
            }}
          >
            📋 Copy Filtered
          </button>

          <button
            onClick={handleCopyReport}
            title="Copy sanitized diagnostic error report to clipboard"
            style={{
              padding: "4px 8px",
              borderRadius: "4px",
              fontSize: "11px",
              border: "1px solid var(--line-10)",
              backgroundColor: "var(--surface-05)",
              color: "#cbd5e1",
              cursor: "pointer",
            }}
          >
            📋 Report
          </button>

          <button
            onClick={handleClear}
            title="Clear all log entries"
            style={{
              padding: "4px 8px",
              borderRadius: "4px",
              fontSize: "11px",
              border: "1px solid var(--line-10)",
              backgroundColor: "var(--surface-05)",
              color: "#cbd5e1",
              cursor: "pointer",
            }}
          >
            🗑️ Clear
          </button>

          {!standalone && (
            <button
              onClick={() => {
                if (onPopOut) {
                  onPopOut();
                } else {
                  void openDevLogsWindow();
                }
              }}
              title="Pop out to separate native window"
              style={{
                padding: "4px 8px",
                borderRadius: "4px",
                fontSize: "11px",
                border: "1px solid rgba(59,130,246,0.4)",
                backgroundColor: "rgba(59,130,246,0.1)",
                color: "#60a5fa",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                gap: "4px",
              }}
            >
              <span>↗</span> Pop Out
            </button>
          )}

          {onClose && (
            <button
              onClick={onClose}
              title="Close log drawer"
              style={{
                padding: "4px 8px",
                borderRadius: "4px",
                fontSize: "11px",
                border: "none",
                backgroundColor: "transparent",
                color: "#94a3b8",
                cursor: "pointer",
              }}
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* Process & Action Diagnostics Strip */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "6px",
          padding: "6px 12px",
          backgroundColor: "#070b14",
          borderBottom: "1px solid var(--line-06)",
          overflowX: "auto",
          flexShrink: 0,
        }}
      >
        <span style={{ fontSize: "10px", fontWeight: 700, color: "var(--ink-40)", textTransform: "uppercase", letterSpacing: "0.05em", marginRight: "4px" }}>
          Process Diagnostics:
        </span>
        {PROCESS_CATEGORIES.map((cat) => {
          const isSelected = selectedProcessCategory === cat.id;
          return (
            <button
              key={cat.id}
              onClick={() => setSelectedProcessCategory(cat.id)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "4px",
                padding: "3px 8px",
                borderRadius: "4px",
                fontSize: "10.5px",
                fontWeight: 600,
                border: isSelected ? "1px solid rgba(16,185,129,0.5)" : "1px solid rgba(255,255,255,0.07)",
                backgroundColor: isSelected ? "rgba(16,185,129,0.18)" : "rgba(255,255,255,0.02)",
                color: isSelected ? "var(--success)" : "rgba(255,255,255,0.6)",
                cursor: "pointer",
                whiteSpace: "nowrap",
                transition: "all 0.12s ease",
              }}
            >
              <span>{cat.icon}</span>
              <span>{cat.label}</span>
            </button>
          );
        })}
      </div>

      {/* Toast Notification Banner */}
      {copiedNotification && (
        <div
          style={{
            padding: "4px 12px",
            backgroundColor: "#059669",
            color: "#ffffff",
            fontSize: "11px",
            fontWeight: 500,
            textAlign: "center",
            transition: "all 0.2s",
          }}
        >
          {copiedNotification}
        </div>
      )}

      {/* Log List View */}
      <div
        ref={scrollContainerRef}
        onScroll={handleScroll}
        style={{
          flex: 1,
          overflowY: "auto",
          overflowX: "hidden",
          padding: "4px 0",
        }}
      >
        {filteredEvents.length === 0 ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              height: "100%",
              color: "#64748b",
              fontSize: "12px",
              fontStyle: "italic",
            }}
          >
            No matching log events recorded
          </div>
        ) : (
          filteredEvents.map((ev) => {
            const isExpanded = expandedRowId === ev.id;
            const lvl = ev.level.toUpperCase();
            const colors = LEVEL_COLORS[lvl] || LEVEL_COLORS.INFO;

            return (
              <div
                key={ev.id}
                style={{
                  borderBottom: "1px solid var(--line-05)",
                  backgroundColor: isExpanded ? "rgba(255,255,255,0.04)" : "transparent",
                }}
              >
                {/* Row Header */}
                <div
                  onClick={() => setExpandedRowId(isExpanded ? null : ev.id)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                    padding: "4px 12px",
                    cursor: "pointer",
                    userSelect: "none",
                    transition: "background-color 0.1s",
                  }}
                  onMouseEnter={(e) => {
                    if (!isExpanded) e.currentTarget.style.backgroundColor = "rgba(255,255,255,0.02)";
                  }}
                  onMouseLeave={(e) => {
                    if (!isExpanded) e.currentTarget.style.backgroundColor = "transparent";
                  }}
                >
                  <span style={{ color: "#475569", fontSize: "10px", width: "12px" }}>
                    {isExpanded ? "▼" : "▶"}
                  </span>
                  <span style={{ color: "#64748b", fontSize: "11px", whiteSpace: "nowrap" }}>
                    {formatTimestamp(ev.timestamp)}
                  </span>
                  <span
                    style={{
                      padding: "1px 6px",
                      borderRadius: "3px",
                      fontSize: "10px",
                      fontWeight: 700,
                      backgroundColor: colors.bg,
                      color: colors.text,
                      border: `1px solid ${colors.border}`,
                      whiteSpace: "nowrap",
                    }}
                  >
                    {lvl}
                  </span>
                  <span
                    style={{
                      color: "#38bdf8",
                      fontSize: "11px",
                      fontWeight: 600,
                      whiteSpace: "nowrap",
                    }}
                  >
                    [{ev.subsystem}]
                  </span>
                  <span
                    style={{
                      flex: 1,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      color: colors.text === "var(--danger)" ? "#fca5a5" : "#e2e8f0",
                    }}
                  >
                    {ev.message}
                  </span>
                  {ev.duration_ms != null && (
                    <span
                      style={{
                        fontSize: "10px",
                        color: "#94a3b8",
                        backgroundColor: "var(--surface-05)",
                        padding: "1px 5px",
                        borderRadius: "3px",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {ev.duration_ms}ms
                    </span>
                  )}
                </div>

                {/* Expanded Details Accordion */}
                {isExpanded && (
                  <div
                    style={{
                      padding: "8px 16px 12px 36px",
                      backgroundColor: "rgba(0,0,0,0.25)",
                      borderTop: "1px solid var(--line-05)",
                      display: "flex",
                      flexDirection: "column",
                      gap: "8px",
                    }}
                  >
                    {/* Error & Traceback Box */}
                    {ev.error && (
                      <div
                        style={{
                          backgroundColor: "#180e12",
                          border: "1px solid rgba(239, 68, 68, 0.3)",
                          borderRadius: "4px",
                          padding: "8px 12px",
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            justifyContent: "space-between",
                            alignItems: "center",
                            marginBottom: "4px",
                          }}
                        >
                          <span style={{ color: "#ef4444", fontWeight: 700, fontSize: "11px" }}>
                            {ev.error.type}: {ev.error.message}
                          </span>
                          {ev.error.traceback && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                void copyText(ev.error!.traceback!, "Traceback");
                              }}
                              style={{
                                background: "none",
                                border: "1px solid rgba(239, 68, 68, 0.4)",
                                color: "#fca5a5",
                                borderRadius: "3px",
                                fontSize: "10px",
                                padding: "2px 6px",
                                cursor: "pointer",
                              }}
                            >
                              Copy Traceback
                            </button>
                          )}
                        </div>
                        {ev.error.traceback && (
                          <pre
                            style={{
                              margin: 0,
                              color: "var(--danger)",
                              fontSize: "11px",
                              lineHeight: 1.4,
                              overflowX: "auto",
                              whiteSpace: "pre-wrap",
                            }}
                          >
                            {ev.error.traceback}
                          </pre>
                        )}
                      </div>
                    )}

                    {/* Structured Context JSON */}
                    {ev.context && Object.keys(ev.context).length > 0 && (
                      <div
                        style={{
                          backgroundColor: "#0d1527",
                          border: "1px solid rgba(59, 130, 246, 0.25)",
                          borderRadius: "4px",
                          padding: "8px 12px",
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            justifyContent: "space-between",
                            alignItems: "center",
                            marginBottom: "4px",
                          }}
                        >
                          <span style={{ color: "#60a5fa", fontWeight: 600, fontSize: "11px" }}>
                            Context Metadata
                          </span>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              void copyText(JSON.stringify(ev.context, null, 2), "Context JSON");
                            }}
                            style={{
                              background: "none",
                              border: "1px solid rgba(59, 130, 246, 0.4)",
                              color: "#93c5fd",
                              borderRadius: "3px",
                              fontSize: "10px",
                              padding: "2px 6px",
                              cursor: "pointer",
                            }}
                          >
                            Copy JSON
                          </button>
                        </div>
                        <pre
                          style={{
                            margin: 0,
                            color: "#93c5fd",
                            fontSize: "11px",
                            lineHeight: 1.4,
                            overflowX: "auto",
                          }}
                        >
                          {JSON.stringify(ev.context, null, 2)}
                        </pre>
                      </div>
                    )}

                    {/* Raw Event Copier */}
                    <div style={{ display: "flex", gap: "8px", marginTop: "2px" }}>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          void copyText(JSON.stringify(ev, null, 2), "Raw Event");
                        }}
                        style={{
                          background: "none",
                          border: "1px solid var(--line-10)",
                          color: "#94a3b8",
                          borderRadius: "3px",
                          fontSize: "10px",
                          padding: "2px 6px",
                          cursor: "pointer",
                        }}
                      >
                        Copy Raw Event JSON
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
