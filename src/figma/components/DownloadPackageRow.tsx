import React, { useState, useMemo } from "react";
import { HostCell, ProgressCell, SizeCell, progressTone } from "../ui/DownloadCells";
import { KindTile } from "../ui/KindTile";
import type { DownloadWithHistory } from "../types";
import {
  aggregatePackageStatus,
  fmtBytes,
  isArchiveDrivenStage,
  isGenuinelyDownloading,
  packageProgress,
  packageStageCarrier,
  pct,
} from "../types";
import { ColumnCells, type ColumnLayout } from "../ui/ColumnHeader";
import { Icon, ic } from "../icons";
import { StatusBadge } from "../ui/StatusBadge";
import { TypeChip } from "../ui/TypeChip";

interface DownloadPackageRowProps {
  packageName: string;
  tasks: DownloadWithHistory[];
  selectedIds: string[];
  focusedId: string | null;
  onSelectRow: (e: React.MouseEvent, id: string) => void;
  onFocusRow: (id: string) => void;
  onOpenSheet: (id: string) => void;
  onTaskAction?: (task: DownloadWithHistory, action: "pause" | "resume" | "stop" | "restart") => void;
  onBulkAction?: (action: "pause" | "resume" | "stop" | "restart", ids: string[]) => void;
  onRemoveTask: (id: string) => void;
  onRemovePackage: (tasks: DownloadWithHistory[]) => void;
  onSelectAllPackage: (tasks: DownloadWithHistory[], select: boolean) => void;
  onContextMenu: (e: React.MouseEvent, task: DownloadWithHistory, packageTaskIds?: string[]) => void;
  showSpeeds?: boolean;
  columns: ColumnLayout;
  /**
   * Expansion state, owned by the parent.
   *
   * Packages are rendered inside status groups (Transferring / Waiting /
   * Completed / Failed). When a package's derived status changes it moves
   * between groups, so React unmounts and remounts this row -- discarding any
   * local expansion state and snapping an open package shut mid-download.
   * Hoisting it keeps the row open across that move. Falls back to local state
   * when the parent does not supply it.
   */
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
}

export const DownloadPackageRow: React.FC<DownloadPackageRowProps> = ({
  packageName,
  tasks,
  selectedIds,
  focusedId,
  onSelectRow,
  onFocusRow,
  onOpenSheet,
  onTaskAction,
  onBulkAction,
  onRemoveTask,
  onRemovePackage,
  onSelectAllPackage,
  onContextMenu,
  showSpeeds = true,
  columns,
  collapsed: collapsedProp,
  onToggleCollapsed,
}) => {
  // Subordinate files start fully collapsed by default per user specification.
  // Local state is only the fallback; see `collapsed` in the props docstring for
  // why the parent normally owns this.
  const [localCollapsed, setLocalCollapsed] = useState(true);
  const controlled = collapsedProp !== undefined;
  const collapsed = controlled ? collapsedProp : localCollapsed;
  const toggleCollapsed = () => {
    if (controlled) onToggleCollapsed?.();
    else setLocalCollapsed((p) => !p);
  };

  // Sort tasks by filename / part number
  const sortedTasks = useMemo(() => {
    return [...tasks].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  }, [tasks]);

  const totalParts = sortedTasks.length;
  const selectedInPackage = sortedTasks.filter((t) => selectedIds.includes(t.id));
  const isAllSelected = selectedInPackage.length === totalParts && totalParts > 0;
  const isPartiallySelected = selectedInPackage.length > 0 && !isAllSelected;

  // Stage-aware aggregate metrics. packageProgress keeps MB units at the call
  // site and converts the engine's byte-level stage_detail internally.
  const progress = useMemo(() => packageProgress(sortedTasks), [sortedTasks]);
  const totalSize = progress.totalMB;
  const totalSpeed = progress.speedMBps;
  const aggregatePct = progress.percent;

  const overallStatus = useMemo(() => aggregatePackageStatus(sortedTasks), [sortedTasks]);
  const anyNeedsUser = overallStatus === "needs_user";
  const anyPaused = overallStatus === "paused";
  const anyError = overallStatus === "error";
  const activeTask = sortedTasks.find((t) => isGenuinelyDownloading(t));
  const archiveTask = sortedTasks.find((t) => isArchiveDrivenStage(t) || t.rawState === "postprocessing");
  const packageComplete = sortedTasks.every((t) => t.status === "completed") && !archiveTask;
  const genuinelyDownloading = !!activeTask;


  // Aggregate ETA: only meaningful while parts are genuinely downloading.
  const etaText =
    genuinelyDownloading && totalSpeed > 0 && progress.remainingMB > 0
      ? (() => {
          const secs = Math.ceil(progress.remainingMB / totalSpeed);
          if (secs < 60) return `${secs}s`;
          if (secs < 3600) return `${Math.floor(secs / 60)}m`;
          return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
        })()
      : packageComplete
      ? "Done"
      : "—";

  const handleMasterCheckboxClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onSelectAllPackage(sortedTasks, !isAllSelected);
  };

  const handleTogglePackage = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (overallStatus === "downloading") {
      const targets = sortedTasks.filter((t) => isGenuinelyDownloading(t));
      if (onBulkAction && targets.length > 0) {
        onBulkAction("pause", targets.map((t) => t.id));
      } else {
        targets.forEach((t) => onTaskAction?.(t, "pause"));
      }
    } else if (overallStatus === "error") {
      const targets = sortedTasks.filter((t) => t.status === "error" || t.status === "cancelled");
      if (onBulkAction && targets.length > 0) {
        onBulkAction("restart", targets.map((t) => t.id));
      } else {
        targets.forEach((t) => onTaskAction?.(t, "restart"));
      }
    } else {
      const targets = sortedTasks.filter((t) => t.status === "paused" || t.status === "queued");
      if (onBulkAction && targets.length > 0) {
        onBulkAction("resume", targets.map((t) => t.id));
      } else {
        targets.forEach((t) => onTaskAction?.(t, "resume"));
      }
    }
  };

  const resolvingTask = sortedTasks.find((t) =>
    (t.stage && ["resolving_metadata", "hoster_wait_timer", "captcha_challenge_detected", "captcha_solving", "captcha_verifying", "direct_link_acquired"].includes(t.stage)) ||
    t.rawState === "resolving"
  );
  const activeIndex = activeTask ? sortedTasks.indexOf(activeTask) + 1 : 0;
  const statusDetail = anyNeedsUser
    ? "Verification Required"
    : activeTask
    ? (activeTask.detailedStatus && activeTask.detailedStatus.includes("Waiting for slot")
        ? `Primed (${activeIndex}/${totalParts})`
        : `Downloading (${activeIndex}/${totalParts})`)
    : resolvingTask
    ? (resolvingTask.detailedStatus || "Pre-resolving...")
    : archiveTask
    ? (archiveTask.detailedStatus ||
        (archiveTask.stage === "unraring_extracting"
          ? "Extracting..."
          : archiveTask.stage === "archive_cleanup"
          ? "Cleaning up archives"
          : "Waiting for archive parts"))
    : packageComplete
    ? "All Parts Completed"
    : anyPaused
    ? "Paused"
    : anyError
    ? "Error in Part"
    : "Queued";
  // Canonical lifecycle stage surfaced on the package header. A running hoster
  // countdown outranks an active transfer: it is transient, time-critical and
  // shown nowhere else on a collapsed package, whereas "Downloading" is already
  // conveyed by the progress bar and speed column. Preferring the transfer meant
  // that once part 1 started downloading, every sibling's countdown became
  // invisible unless the package happened to be expanded.
  const stageCarrier = packageStageCarrier(sortedTasks);
  const activeStage = stageCarrier?.stage;
  const activeStageDetail = stageCarrier?.stageDetail;
  const activeStageEnteredAt = stageCarrier?.stageEnteredAt;

  return (
    <div style={{ display: "flex", flexDirection: "column", marginBottom: "3px" }}>
      {/* â”€â”€ Package Header Row â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */}
      <div
        className={`dl-row group${isAllSelected ? " selected" : ""}`}
        style={{
          background: isAllSelected
            ? "rgba(0, 120, 212, 0.14)"
            : isPartiallySelected
            ? "rgba(0, 120, 212, 0.07)"
            : "rgba(255, 255, 255, 0.035)",
          border: isAllSelected
            ? "1px solid rgba(0, 120, 212, 0.4)"
            : isPartiallySelected
            ? "1px solid rgba(0, 120, 212, 0.25)"
            : "1px solid rgba(255, 255, 255, 0.07)",
          borderRadius: collapsed ? "5px" : "5px 5px 0 0",
          cursor: "pointer",
          userSelect: "none",
        }}
        onClick={(e) => {
          e.stopPropagation();
          if (e.shiftKey) {
            // Shift-click: select all tasks in this package
            onSelectAllPackage(sortedTasks, !isAllSelected);
          } else {
            toggleCollapsed();
          }
        }}
        onContextMenu={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onContextMenu(e, sortedTasks[0], sortedTasks.map((t) => t.id));
        }}
      >
        {/* Progress Fill Background */}
        <div className="dl-row-progress" style={{ height: "2px" }}>
          <div
            className={`dl-row-progress-fill ${overallStatus === "downloading" ? "progress-shimmer" : ""}`}
            style={{ width: `${aggregatePct}%` }}
          />
        </div>

        <ColumnCells
          layout={columns}
          cells={{
            select: (
              <button
                type="button"
                className={`download-select-trigger${isAllSelected || isPartiallySelected ? " selected" : ""}`}
                aria-label={`${isAllSelected ? "Deselect" : "Select"} ${packageName}`}
                onClick={handleMasterCheckboxClick}
                title={isAllSelected ? "Deselect all parts" : "Select all parts"}
              >
                {isAllSelected ? (
                  <Icon d={ic.check} size={11} />
                ) : isPartiallySelected ? (
                  <div style={{ width: "8px", height: "2px", background: "var(--accent-light)", borderRadius: "1px" }} />
                ) : (
                   <KindTile category="archives" size={20} />
                )}
              </button>
            ),
            name: (
              <div className="flex items-center gap-2 min-w-0">
                <span style={{ color: "var(--ink-45)", flexShrink: 0, display: "flex", alignItems: "center" }}>
                  <Icon d={collapsed ? ic.chevronRight : ic.chevronDown} size={12} />
                </span>
                <span style={{ color: "var(--accent-light)", display: "flex", alignItems: "center", flexShrink: 0 }}>
                  <Icon d={ic.folder} size={14} />
                </span>
                <span className="truncate font-semibold" style={{ fontSize: "11px" }} title={`${packageName} (${totalParts} parts)`}>
                  {packageName}
                </span>
                <span
                  style={{
                    fontSize: "9px",
                    fontWeight: 700,
                    padding: "1px 5px",
                    borderRadius: "3px",
                    background: "rgba(96, 165, 250, 0.15)",
                    color: "var(--accent-light)",
                    fontFamily: "var(--font-mono)",
                    flexShrink: 0,
                  }}
                >
                  {totalParts} Parts
                </span>
                <span style={{ fontSize: "8.5px", color: "var(--ink-35)", fontFamily: "var(--font-mono)", flexShrink: 0 }}>
                  ({selectedInPackage.length}/{totalParts})
                </span>
              </div>
            ),
            status: (
              <StatusBadge
                status={overallStatus as any}
                rawState={stageCarrier?.rawState}
                detailedStatus={statusDetail}
                stage={activeStage}
                stageDetail={activeStageDetail}
                stageEnteredAt={activeStageEnteredAt}
              />
            ),
            type: <TypeChip type="ARCHIVE" />,
            speed: showSpeeds ? (
              <span style={{ fontSize: "9px", color: genuinelyDownloading ? "var(--success)" : "var(--ink-30)", fontFamily: "var(--font-mono)" }}>
                {genuinelyDownloading ? `${fmtBytes(totalSpeed)}/s` : "—"}
              </span>
            ) : null,
            eta: (
              <span style={{ fontSize: "9px", color: "var(--ink-35)", fontFamily: "var(--font-mono)" }}>{etaText}</span>
            ),
            pct: <ProgressCell percent={aggregatePct} tone={progressTone(overallStatus)} />,
            size: <SizeCell done={totalSize * aggregatePct / 100} total={totalSize} showDone={overallStatus !== "completed" && aggregatePct > 0} />,
            host: <HostCell url={sortedTasks[0]?.url} />,
            added: (
              <span className="truncate" style={{ fontSize: "9.5px", color: "var(--ink-35)", fontFamily: "var(--font-mono)" }}>
                {sortedTasks[0]?.added ?? "—"}
              </span>
            ),
            folder: (
              <span className="truncate" style={{ fontSize: "9.5px", color: "var(--ink-35)" }} title={sortedTasks[0]?.destination}>
                {sortedTasks[0]?.destination || "—"}
              </span>
            ),
            actions: (
              <div
                className="flex items-center gap-1 justify-end opacity-0 group-hover:opacity-100 transition-opacity"
                onClick={(e) => e.stopPropagation()}
              >
                <button
                  onClick={handleTogglePackage}
                  className="w-5 h-5 rounded flex items-center justify-center row-action-btn row-action-btn-primary"
                  title={genuinelyDownloading ? "Pause all parts" : "Resume all parts"}
                >
                  <Icon d={genuinelyDownloading ? ic.pause : ic.play} size={9} />
                </button>
                <button
                  onClick={() => onRemovePackage(sortedTasks)}
                  className="w-5 h-5 rounded flex items-center justify-center row-action-btn row-action-btn-danger"
                  title="Delete package"
                >
                  <Icon d={ic.trash} size={9} />
                </button>
              </div>
            ),
          }}
        />
      </div>

      {/* â”€â”€ Subordinate Part Files (Compact Small Rows) â”€â”€â”€â”€â”€â”€â”€ */}
      {!collapsed && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            background: "rgba(0, 0, 0, 0.15)",
            boxShadow: "inset 2px 0 0 rgba(0, 120, 212, 0.35), inset -1px -1px 0 var(--line-05)",
            borderRadius: "0 0 5px 5px",
            padding: "2px 0 3px",
            gap: "1px",
          }}
        >
          {sortedTasks.map((d, idx) => {
            const p = pct(d);
            const isSelected = selectedIds.includes(d.id);
            const isFocused = focusedId === d.id;
            const partNum = idx + 1;
            const formattedPart = String(partNum).padStart(2, "0");

            return (
              <div
                key={d.id}
                className={`dl-row-small group${isSelected ? " selected" : ""}${isFocused ? " focused" : ""}`}
                role="row"
                aria-selected={isSelected}
                style={{ borderRadius: "3px", userSelect: "none" }}
                onClick={(e) => {
                  e.stopPropagation();
                  e.ctrlKey || e.metaKey || e.shiftKey ? onSelectRow(e, d.id) : onFocusRow(d.id);
                }}
                onDoubleClick={(e) => {
                  e.stopPropagation();
                  onOpenSheet(d.id);
                }}
                onContextMenu={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  onContextMenu(e, d);
                }}
              >
                <div className="dl-row-progress" style={{ height: "1px" }}>
                  <div
                    className={`dl-row-progress-fill ${isGenuinelyDownloading(d) ? "progress-shimmer" : ""}`}
                    style={{ width: `${p}%` }}
                  />
                </div>

                <ColumnCells
                  layout={columns}
                  cells={{
                    select: (
                      <button
                        type="button"
                        className={`download-select-trigger${isSelected ? " selected" : ""}`}
                        aria-label={`${isSelected ? "Deselect" : "Select"} ${d.name}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelectRow(e, d.id);
                        }}
                      >
                        {isSelected ? (
                          <Icon d={ic.check} size={9} />
                        ) : (
                          <span style={{ fontSize: "8px", color: "var(--ink-40)" }}>{formattedPart}</span>
                        )}
                      </button>
                    ),
                    name: (
                      <div className="flex items-center gap-1.5 min-w-0" style={{ paddingLeft: "14px" }}>
                        <span
                          style={{
                            fontSize: "8.5px",
                            fontWeight: 700,
                            padding: "1px 4px",
                            borderRadius: "2px",
                            background: "var(--surface-06)",
                            color: "var(--ink-60)",
                            fontFamily: "var(--font-mono)",
                            flexShrink: 0,
                          }}
                        >
                          P{formattedPart}
                        </span>
                        <span
                          className="truncate font-medium"
                          style={{ fontSize: "10px", color: isSelected ? "var(--ink-90)" : "var(--ink-70)", fontFamily: "var(--font-mono)" }}
                          title={d.name}
                        >
                          {d.name}
                        </span>
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
                    speed: showSpeeds ? (
                      <span style={{ fontSize: "8.5px", color: isGenuinelyDownloading(d) ? "var(--success)" : "var(--ink-25)", fontFamily: "var(--font-mono)" }}>
                        {isGenuinelyDownloading(d) ? `${fmtBytes(d.speed)}/s` : d.status === "paused" ? "Paused" : "—"}
                      </span>
                    ) : null,
                    eta: (
                      <span style={{ fontSize: "8.5px", color: "var(--ink-35)", fontFamily: "var(--font-mono)" }}>
                        {isGenuinelyDownloading(d) ? d.eta : "—"}
                      </span>
                    ),
                    pct: <ProgressCell percent={p} tone={progressTone(d.status)} />,
                    size: <SizeCell done={d.downloaded} total={d.size} showDone={d.status !== "completed" && d.downloaded > 0} />,
                    host: <HostCell url={d.url} />,
                    added: (
                      <span className="truncate" style={{ fontSize: "9px", color: "var(--ink-35)", fontFamily: "var(--font-mono)" }}>{d.added}</span>
                    ),
                    folder: (
                      <span className="truncate" style={{ fontSize: "9px", color: "var(--ink-35)" }} title={d.destination}>
                        {d.destination || "—"}
                      </span>
                    ),
                    actions: (
                      <div
                        className="flex items-center gap-1 justify-end opacity-0 group-hover:opacity-100 transition-opacity"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {d.status === "error" || d.status === "cancelled" ? (
                          <button
                            onClick={() => onTaskAction?.(d, "restart")}
                            className="w-4 h-4 rounded flex items-center justify-center"
                            style={{ background: "rgba(239,68,68,0.2)", color: "var(--danger)" }}
                            title="Retry part"
                          >
                            <Icon d={ic.refreshCw} size={8} />
                          </button>
                        ) : d.status === "downloading" || d.status === "paused" || d.status === "queued" ? (
                          <button
                            onClick={() => onTaskAction?.(d, isGenuinelyDownloading(d) ? "pause" : "resume")}
                            className="w-4 h-4 rounded flex items-center justify-center row-action-btn row-action-btn-primary"
                            title={isGenuinelyDownloading(d) ? "Pause" : "Resume"}
                          >
                            <Icon d={isGenuinelyDownloading(d) ? ic.pause : ic.play} size={8} />
                          </button>
                        ) : null}
                        <button
                          onClick={() => onRemoveTask(d.id)}
                          className="w-4 h-4 rounded flex items-center justify-center row-action-btn row-action-btn-danger"
                          title="Remove part"
                        >
                          <Icon d={ic.trash} size={8} />
                        </button>
                      </div>
                    ),
                  }}
                />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
