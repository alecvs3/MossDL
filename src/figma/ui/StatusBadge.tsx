import React, { useEffect, useState } from "react";
import { stageCountdownRemaining, type DownloadStatus, type HistoryStatus } from "../types";
import type { TaskState } from "../../api";

interface StatusBadgeProps {
  status: DownloadStatus | HistoryStatus;
  rawState?: TaskState;
  detailedStatus?: string;
  stage?: string;
  stageDetail?: Record<string, unknown>;
  stageEnteredAt?: number;
  error?: string;
  pausedReason?: string;
}

// Canonical lifecycle stage presentation. Colors key off the engine-owned
// stage taxonomy; detailedStatus is display text only, never matched on.
const STAGE_STYLE: Record<string, { c: string; bg: string; dot: boolean }> = {
  resolving_metadata: { c: "#38bdf8", bg: "rgba(56, 189, 248, 0.16)", dot: true },
  hoster_wait_timer: { c: "#fbbf24", bg: "rgba(251, 191, 36, 0.18)", dot: true },
  captcha_challenge_detected: { c: "#a78bfa", bg: "rgba(167, 139, 250, 0.18)", dot: true },
  captcha_solving: { c: "#a78bfa", bg: "rgba(167, 139, 250, 0.18)", dot: true },
  captcha_verifying: { c: "#a78bfa", bg: "rgba(167, 139, 250, 0.18)", dot: true },
  direct_link_acquired: { c: "var(--success)", bg: "rgba(52, 211, 153, 0.18)", dot: true },
  downloading: { c: "var(--accent-light)", bg: "rgba(0,120,212,0.18)", dot: true },
  verifying_integrity: { c: "#a78bfa", bg: "rgba(167, 139, 250, 0.18)", dot: true },
  unraring_pending: { c: "#c084fc", bg: "rgba(192, 132, 252, 0.18)", dot: true },
  unraring_extracting: { c: "#c084fc", bg: "rgba(192, 132, 252, 0.18)", dot: true },
  archive_cleanup: { c: "#c084fc", bg: "rgba(192, 132, 252, 0.18)", dot: true },
  completed: { c: "var(--success)", bg: "rgba(20,174,92,0.14)", dot: false },
  failed: { c: "var(--danger)", bg: "rgba(239,68,68,0.16)", dot: false },
};

export function StatusBadge({ status, rawState, detailedStatus, stage, stageDetail, stageEnteredAt, error, pausedReason }: StatusBadgeProps) {
  // Tick once per second only while projecting the engine-timed wait stage.
  const [clock, setClock] = useState(() => Date.now() / 1000);
  const countingDown = !!rawState && stage === "hoster_wait_timer";
  useEffect(() => {
    if (!countingDown) return;
    const timer = window.setInterval(() => setClock(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [countingDown]);

  let c = "var(--accent-light)";
  let bg = "rgba(0,120,212,0.18)";
  let label = "Downloading";
  let dot = false;

  if (rawState) {
    // Live countdown anchored to the engine stage entry timestamp.
    if (stage === "hoster_wait_timer") {
      const remaining = stageCountdownRemaining({ stage, stageDetail, stageEnteredAt }, clock);
      c = "#fbbf24";
      bg = "rgba(251, 191, 36, 0.18)";
      label = remaining !== null ? `Waiting timer (${remaining}s)...` : "Waiting timer...";
      dot = true;
    } else if (stage && STAGE_STYLE[stage]) {
      const style = STAGE_STYLE[stage];
      c = style.c;
      bg = style.bg;
      dot = style.dot;
      label = detailedStatus || stage;
    } else {
      switch (rawState) {
        case "pending_probe":
          c = "var(--ink-45)";
          bg = "var(--surface-08)";
          label = detailedStatus || "Waiting for Part 1";
          dot = false;
          break;
        case "resolving":
          c = "#38bdf8";
          bg = "rgba(56, 189, 248, 0.16)";
          label = detailedStatus || "Getting metadata";
          dot = true;
          break;
        case "preflight":
          c = "#38bdf8";
          bg = "rgba(56, 189, 248, 0.16)";
          label = detailedStatus || "Preparing";
          dot = true;
          break;
        case "queued":
          c = "var(--ink-45)";
          bg = "var(--surface-08)";
          label = detailedStatus || "Starting";
          dot = false;
          break;
        case "downloading":
          c = "var(--accent-light)";
          bg = "rgba(0,120,212,0.18)";
          label = detailedStatus || "Downloading";
          dot = true;
          break;
        case "verifying":
          c = "#a78bfa";
          bg = "rgba(167, 139, 250, 0.18)";
          label = detailedStatus || "Verifying";
          dot = true;
          break;
        case "postprocessing":
          c = "#c084fc";
          bg = "rgba(192, 132, 252, 0.18)";
          label = detailedStatus || "Extracting";
          dot = true;
          break;
        case "needs_user":
          c = "#fbbf24";
          bg = "rgba(251, 191, 36, 0.18)";
          label = detailedStatus || "CAPTCHA";
          dot = true;
          break;
        case "retrying":
          c = "#f97316";
          bg = "rgba(249, 115, 22, 0.18)";
          label = detailedStatus || "Retrying";
          dot = true;
          break;
        case "paused":
          c = "var(--warning)";
          bg = "rgba(245,158,11,0.16)";
          label = detailedStatus || (pausedReason ? `Paused: ${pausedReason}` : "Paused");
          dot = false;
          break;
        case "completed":
          c = "var(--success)";
          bg = "rgba(20,174,92,0.14)";
          label = "Completed";
          dot = false;
          break;
        case "canceled":
          c = "var(--ink-35)";
          bg = "var(--surface-08)";
          label = "Cancelled";
          dot = false;
          break;
        case "failed":
          c = "var(--danger)";
          bg = "rgba(239,68,68,0.16)";
          label = detailedStatus || (error ? `Failed: ${error}` : "Failed");
          dot = false;
          break;
        default:
          label = detailedStatus || rawState;
          break;
      }
    }
  } else {
    const config = {
      downloading: { c: "var(--accent-light)", bg: "rgba(0,120,212,0.18)", label: detailedStatus || "Downloading", dot: true },
      paused: { c: "var(--warning)", bg: "rgba(245,158,11,0.16)", label: "Paused", dot: false },
      queued: { c: "var(--ink-40)", bg: "var(--surface-08)", label: detailedStatus || "Starting", dot: false },
      error: { c: "var(--danger)", bg: "rgba(239,68,68,0.16)", label: "Error", dot: false },
      completed: { c: "var(--success)", bg: "rgba(20,174,92,0.14)", label: "Completed", dot: false },
      cancelled: { c: "var(--ink-35)", bg: "var(--surface-08)", label: "Cancelled", dot: false },
      failed: { c: "var(--danger)", bg: "rgba(239,68,68,0.16)", label: "Failed", dot: false },
      needs_user: { c: "var(--warning)", bg: "rgba(245,158,11,0.16)", label: "Action Needed", dot: true },
    }[status] || { c: "var(--ink-40)", bg: "var(--surface-08)", label: String(status), dot: false };
    c = config.c;
    bg = config.bg;
    label = config.label;
    dot = config.dot;
  }

  const tooltip = error || pausedReason || label;

  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 shrink-0 max-w-full"
      title={tooltip}
      style={{
        background: bg,
        color: c,
        fontSize: "9.5px",
        fontWeight: 600,
        border: `1px solid ${c}22`,
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
      }}
    >
      {dot && <span className="w-1.5 h-1.5 rounded-full pulse-dot shrink-0" style={{ background: c }} />}
      <span className="truncate">{label}</span>
    </span>
  );
}
