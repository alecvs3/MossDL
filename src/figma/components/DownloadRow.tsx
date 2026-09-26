// One download row for every row size. Small, medium and large differ only in
// height and density; all three render through the shared column layout, so
// they honour the same column order, widths and hidden columns as the header.
import React from "react";
import { HostCell, ProgressCell, SizeCell, progressTone } from "../ui/DownloadCells";
import { KindTile } from "../ui/KindTile";
import type { DownloadStatus, DownloadWithHistory } from "../types";
import { fmtBytes, isGenuinelyDownloading, pct } from "../types";
import { Icon, ic, detectProvider, providerLabel } from "../icons";
import { ColumnCells, type ColumnLayout } from "../ui/ColumnHeader";
import { StatusBadge } from "../ui/StatusBadge";
import { TypeChip } from "../ui/TypeChip";
import { Sparkline } from "../ui/Sparkline";

export type RowSize = "small" | "medium" | "large";

export const STATUS_ACCENT: Record<DownloadStatus, string | null> = {
  downloading: "var(--accent-light)",
  paused: "var(--warning)",
  queued: null,
  error: "var(--danger)",
  completed: "var(--success)",
  cancelled: "#94a3b8",
  needs_user: "var(--warning)",
};

export interface RenameState {
  value: string;
  onChange: (value: string) => void;
  onCommit: () => void;
  onCancel: () => void;
}

interface Props {
  d: DownloadWithHistory;
  size: RowSize;
  columns: ColumnLayout;
  showSpeeds: boolean;
  focused: boolean;
  rename: RenameState | null;
  onClick: (e: React.MouseEvent) => void;
  onDoubleClick: (e: React.MouseEvent) => void;
  onContextMenu: (e: React.MouseEvent) => void;
  onSelect: (e: React.MouseEvent) => void;
  onToggle: () => void;
  onRestart: () => void;
  onRemove: () => void;
}

const mono = { fontFamily: "var(--font-mono)", whiteSpace: "nowrap" } as const;

export function DownloadRow({
  d, size, columns, showSpeeds, focused, rename,
  onClick, onDoubleClick, onContextMenu, onSelect, onToggle, onRestart, onRemove,
}: Props) {
  const p = pct(d);
  const small = size === "small";
  const provider = detectProvider(d.url, d.provider);
  const live = isGenuinelyDownloading(d);
  const accent = STATUS_ACCENT[d.status];
  const fill = d.status === "error" ? "rgba(220,80,80,0.55)" : d.status === "paused" ? "rgba(200,140,30,0.5)" : undefined;
  const btn = small ? "w-5 h-5" : "w-6 h-6";
  const iconSize = small ? 9 : 10;

  return (
    <div
      className={`${small ? "dl-row-small" : "dl-row"} group${size === "large" ? " dl-row-large" : ""}${d.selected ? " selected" : ""}${focused ? " focused" : ""}`}
      role="row"
      aria-selected={d.selected}
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      onContextMenu={onContextMenu}
    >
      {accent && <div className="dl-row-accent" style={{ background: accent, boxShadow: small ? undefined : `0 0 4px ${accent}44` }} />}
      <div className="dl-row-progress" style={small ? { height: "1px" } : undefined}>
        <div
          className={`dl-row-progress-fill ${live ? "progress-shimmer" : ""}`}
          style={{ width: `${p}%`, ...(fill ? { background: fill } : {}), boxShadow: live && !small ? "0 0 3px rgba(77,166,245,0.2)" : undefined }}
        />
      </div>
      <ColumnCells
        layout={columns}
        cells={{
          select: (
            <button
              type="button"
              className={`download-select-trigger${d.selected ? " selected" : ""}`}
              aria-label={`${d.selected ? "Deselect" : "Select"} ${d.name}`}
              aria-pressed={d.selected}
              title={d.selected ? "Deselect download" : "Select download"}
              onClick={(e) => { e.stopPropagation(); onSelect(e); }}
            >
              {d.selected ? <Icon d={ic.check} size={small ? 11 : 12} /> : <KindTile category={d.category} size={small ? 18 : 22} />}
            </button>
          ),
          name: rename ? (
            <input
              autoFocus
              className="win-input"
              aria-label="New file name"
              style={{ fontSize: "11px", fontWeight: 600, width: "100%", padding: "1px 4px" }}
              value={rename.value}
              onChange={(e) => rename.onChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") rename.onCommit();
                if (e.key === "Escape") rename.onCancel();
                e.stopPropagation();
              }}
              onBlur={rename.onCommit}
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            <div className="min-w-0">
              <p className="truncate" style={{ fontSize: small ? "10.5px" : "11px", fontWeight: 600, color: "var(--ink-85)", lineHeight: 1 }} title={d.name}>
                {d.name}
              </p>
              {!small && (
                <p className="truncate" style={{ fontSize: "8.5px", color: "var(--ink-20)", marginTop: "2px" }}>
                  {providerLabel(provider)} · {d.added}
                </p>
              )}
            </div>
          ),
          status: (
            <StatusBadge
              status={d.status}
              rawState={d.rawState}
              detailedStatus={d.detailedStatus}
              stage={d.stage}
              stageDetail={d.stageDetail}
              stageEnteredAt={d.stageEnteredAt}
              error={d.error}
              pausedReason={d.pausedReason}
            />
          ),
          type: <TypeChip type={d.type} />,
          speed: !showSpeeds ? null : small || !live ? (
            <span style={{ ...mono, fontSize: "9px", color: live ? accent ?? "var(--ink-30)" : "var(--ink-20)" }}>
              {live ? `${fmtBytes(d.speed)}/s` : d.status === "paused" ? "Paused" : "—"}
            </span>
          ) : (
            <Sparkline history={d.speedHistory} color="var(--success)" />
          ),
          eta: <span style={{ ...mono, fontSize: "9px", color: "var(--ink-30)" }}>{live ? d.eta : "—"}</span>,
          pct: <ProgressCell percent={p} tone={progressTone(d.status)} />,
          size: <SizeCell done={d.downloaded} total={d.size} showDone={d.status !== "completed" && d.downloaded > 0} />,
          host: <HostCell url={d.url} />,
          added: <span className="truncate" style={{ ...mono, fontSize: "9.5px", color: "var(--ink-35)" }}>{d.added}</span>,
          folder: <span className="truncate" style={{ fontSize: "9.5px", color: "var(--ink-35)" }} title={d.destination}>{d.destination || "—"}</span>,
          actions: (
            <div className="flex items-center gap-1 justify-end opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
              {d.status === "error" || d.status === "cancelled" ? (
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); onRestart(); }}
                  className={`${btn} rounded flex items-center justify-center transition-colors`}
                  style={{ background: "rgba(239,68,68,0.15)", color: "var(--danger)" }}
                  title="Retry download"
                  aria-label={`Retry ${d.name}`}
                >
                  <Icon d={ic.refreshCw} size={iconSize} />
                </button>
              ) : d.status === "downloading" || d.status === "paused" || d.status === "queued" ? (
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); onToggle(); }}
                  className={`${btn} rounded flex items-center justify-center row-action-btn row-action-btn-primary`}
                  title={live ? "Pause" : "Resume"}
                  aria-label={`${live ? "Pause" : "Resume"} ${d.name}`}
                >
                  <Icon d={live ? ic.pause : ic.play} size={iconSize} />
                </button>
              ) : null}
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); onRemove(); }}
                className={`${btn} rounded flex items-center justify-center row-action-btn row-action-btn-danger`}
                title="Remove download"
                aria-label={`Remove ${d.name}`}
              >
                <Icon d={ic.trash} size={iconSize} />
              </button>
            </div>
          ),
        }}
      />
    </div>
  );
}
