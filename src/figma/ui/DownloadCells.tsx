// Cells shared by download rows and multi-part package rows, so a package
// and a single file show progress, size and host the same way.
import React from "react";
import { fmtBytes } from "../types";
import { SiteIcon, hostOfUrl } from "./SiteIcon";

export function ProgressCell({ percent, tone }: { percent: number; tone?: "active" | "paused" | "error" | "done" }) {
  const p = Math.max(0, Math.min(100, Math.round(percent)));
  return (
    <span className="dl-progress-cell" title={`${p}%`}>
      <span className={`dl-progress-bar${tone ? ` ${tone}` : ""}`} aria-hidden="true"><span style={{ width: `${p}%` }} /></span>
      <span className="dl-progress-pct">{p > 0 ? `${p}%` : "—"}</span>
    </span>
  );
}

/** The done part without its unit when both share one: "6.48 / 12.00 GB". */
function compactDone(done: number, total: number): string {
  const d = fmtBytes(done), t = fmtBytes(total);
  const unit = (x: string) => x.split(" ")[1];
  return unit(d) === unit(t) ? d.split(" ")[0] : d;
}

/** "892 MB / 2.1 GB" while in progress, the total once done or not started. Sizes in MB (view model units). */
export function SizeCell({ done, total, showDone }: { done: number; total: number; showDone: boolean }) {
  return (
    <span className="dl-size-cell">
      {showDone && total > 0 ? <><span className="dl-size-done">{compactDone(done, total)}</span> / {fmtBytes(total)}</> : total > 0 ? fmtBytes(total) : "—"}
    </span>
  );
}

export function HostCell({ url }: { url?: string | null }) {
  const host = hostOfUrl(url);
  return (
    <span className="dl-host-cell" title={url ?? undefined}>
      <SiteIcon url={url} size={14} />
      <span className="truncate">{host || "—"}</span>
    </span>
  );
}

export function progressTone(status: string): "active" | "paused" | "error" | "done" | undefined {
  if (status === "downloading") return "active";
  if (status === "paused" || status === "needs_user") return "paused";
  if (status === "error" || status === "cancelled") return "error";
  if (status === "completed") return "done";
  return undefined;
}
