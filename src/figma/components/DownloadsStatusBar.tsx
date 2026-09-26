// Totals under the Downloads list: how many are in each state, the total
// size, and the combined speed. Every number comes from the engine's tasks.
import React from "react";
import type { DownloadWithHistory } from "../types";
import { fmtBytes, isGenuinelyDownloading } from "../types";
import { Icon, ic } from "../icons";

export function DownloadsStatusBar({ downloads, selectedCount }: { downloads: DownloadWithHistory[]; selectedCount: number }) {
  if (!downloads.length) return null;
  const count = (pred: (d: DownloadWithHistory) => boolean) => downloads.filter(pred).length;
  const active = downloads.filter(isGenuinelyDownloading);
  const parts: [number, string, string?][] = [
    [active.length, "downloading", "active"],
    [count((d) => d.status === "queued"), "queued"],
    [count((d) => d.status === "paused" || d.status === "needs_user"), "paused"],
    [count((d) => d.status === "completed"), "completed"],
    [count((d) => d.status === "error"), "failed", "bad"],
  ];
  const total = downloads.reduce((sum, d) => sum + (d.size || 0), 0);
  const speed = active.reduce((sum, d) => sum + (d.speed || 0), 0);
  return (
    <div className="dl-status" role="status" aria-live="off">
      <span>{downloads.length} item{downloads.length === 1 ? "" : "s"}{selectedCount ? ` (${selectedCount} selected)` : ""}</span>
      {parts.filter(([n]) => n > 0).map(([n, label, tone]) => (
        <span key={label} className={tone ? `dl-status-${tone}` : undefined}>{n} {label}</span>
      ))}
      {total > 0 && <span>{fmtBytes(total)} total</span>}
      <span className="grow" />
      <span className="dl-status-speed" title="Combined download speed">
        <Icon d={ic.download} size={11} /> {speed > 0 ? `${fmtBytes(speed)}/s` : "idle"}
      </span>
    </div>
  );
}
