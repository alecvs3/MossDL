import React from "react";
import type { DownloadWithHistory } from "../types";
import { Icon, ic } from "../icons";
import { canPerformTransferAction, getCommandState, normalizeTaskStatus } from "../commands";

export function GlobalToolbar({
  downloads,
  selectedIds = [],
  onTaskAction,
  onBulkAction,
  onRemove,
  onClearCompleted,
  onAddUrl,
  onOpenFolder,
  onDeduplicate,
}: {
  downloads: DownloadWithHistory[];
  selectedIds?: string[];
  onTaskAction?: (task: DownloadWithHistory, action: "pause" | "resume" | "stop" | "restart") => void;
  onBulkAction?: (action: "pause" | "resume" | "stop" | "restart", ids: string[]) => void;
  onRemove?: (id: string | string[]) => void;
  onClearCompleted?: () => void;
  onAddUrl: () => void;
  onOpenFolder?: (path: string) => void;
  onDeduplicate?: () => void;
}) {
  const { selected, scope, activeIds, resumableIds, pausableIds, failed, folderTarget, dupCount } = React.useMemo(() => {
    const selected = downloads.filter((d) => selectedIds.includes(d.id));
    const scope = selectedIds.length ? selected : downloads;
    const activeIds = scope
      .filter((d) => canPerformTransferAction(normalizeTaskStatus(d.status), "stop"))
      .map((d) => d.id);
    const resumableIds = scope
      .filter((d) => canPerformTransferAction(normalizeTaskStatus(d.status), "resume"))
      .map((d) => d.id);
    const pausableIds = scope
      .filter((d) => canPerformTransferAction(normalizeTaskStatus(d.status), "pause"))
      .map((d) => d.id);
    const failed = scope.filter((d) => d.status === "error");
    const folderTarget = [...selected, ...downloads].find((d) => d.destination)?.destination;

    const seen = new Map<string, number>();
    for (const d of downloads) {
      const key = (d as { source_fingerprint?: string }).source_fingerprint || `${d.url}|${d.size}`;
      seen.set(key, (seen.get(key) ?? 0) + 1);
    }
    const dupCount = [...seen.values()].filter((c) => c > 1).reduce((a, c) => a + c - 1, 0);

    return { selected, scope, activeIds, resumableIds, pausableIds, failed, folderTarget, dupCount };
  }, [downloads, selectedIds]);

  const deleteState = getCommandState("delete-selected", { selectedCount: selectedIds.length, capabilities: {} });
  const [confirmDelete, setConfirmDelete] = React.useState(false);

  const groups: {
    label: string;
    icon: string;
    title: string;
    action?: () => void;
    disabled?: boolean;
    variant: "resume" | "pause" | "stop" | "retry" | "clear" | "folder" | "delete" | "copy" | "add-url" | "dedupe";
    badge?: number;
  }[][] = [
    [
      {
        label: "Resume",
        icon: ic.playAll,
        title: selectedIds.length ? "Resume Selected" : "Resume All",
        action: () => onBulkAction?.("resume", resumableIds),
        disabled: resumableIds.length === 0,
        variant: "resume",
      },
      {
        label: "Pause",
        icon: ic.pauseAll,
        title: selectedIds.length ? "Pause Selected" : "Pause All",
        action: () => onBulkAction?.("pause", pausableIds),
        disabled: pausableIds.length === 0,
        variant: "pause",
      },
      {
        label: "Stop",
        icon: ic.stopAll,
        title: selectedIds.length ? "Stop Selected" : "Stop All",
        action: () => onBulkAction?.("stop", activeIds),
        disabled: activeIds.length === 0,
        variant: "stop",
      },
    ],
    [
      {
        label: "Retry",
        icon: ic.refreshCw,
        title: "Retry Failed",
        action: () => {
          if (onBulkAction) onBulkAction("restart", failed.map((task) => task.id));
          else failed.forEach((task) => onTaskAction?.(task, "restart"));
        },
        disabled: failed.length === 0,
        variant: "retry",
      },
      {
        label: "Dedupe",
        icon: ic.dedupe,
        title: `Remove duplicate downloads${dupCount > 0 ? ` (${dupCount} found)` : ""}`,
        action: () => onDeduplicate?.(),
        disabled: dupCount === 0,
        variant: "dedupe",
        badge: dupCount > 0 ? dupCount : undefined,
      },
      {
        label: "Clear",
        icon: ic.clearDone,
        title: "Clear Completed",
        action: () => onClearCompleted?.(),
        variant: "clear",
      },
      {
        label: "Folder",
        icon: ic.folder,
        title: "Open Folder",
        action: () => {
          if (folderTarget) onOpenFolder?.(folderTarget);
        },
        disabled: !folderTarget || !onOpenFolder,
        variant: "folder",
      },
    ],
    [
      {
        label: confirmDelete ? "Confirm?" : "Delete",
        icon: ic.trash,
        title: confirmDelete
          ? `Click to confirm deleting ${selected.length} download${selected.length === 1 ? "" : "s"}`
          : "Delete Selected",
        action: () => {
          if (!confirmDelete && selected.length > 1) {
            setConfirmDelete(true);
            setTimeout(() => setConfirmDelete(false), 4000);
            return;
          }
          if (selected.length === 1) {
            onRemove?.(selected[0].id);
          } else if (selected.length > 1) {
            onRemove?.(selected.map((task) => task.id));
          }
          setConfirmDelete(false);
        },
        disabled: !deleteState.enabled,
        variant: "delete",
      },
      {
        label: "Copy",
        icon: ic.clipboardList,
        title: "Copy URL",
        action: () => {
          const urls = downloads.filter((d) => d.selected).map((d) => d.url).join("\n");
          if (urls) navigator.clipboard.writeText(urls).catch(() => {});
        },
        disabled: selected.length === 0,
        variant: "copy",
      },
      {
        label: "Add URL",
        icon: ic.plus,
        title: "Add URL",
        action: onAddUrl,
        variant: "add-url",
      },
    ],
  ];

  return (
    <div className="dl-toolbar shrink-0" aria-label="Transfer actions">
      {groups.map((grp, gi) => (
        <React.Fragment key={gi}>
          {gi > 0 && <div className="dl-toolbar-sep" />}
          <div className="dl-toolbar-group">
            {grp.map((b) => (
              <button
                key={b.title}
                title={b.title}
                onClick={b.disabled ? undefined : b.action}
                disabled={b.disabled}
                className={`dl-toolbar-btn btn-${b.variant}`}
                style={{ position: "relative" }}
              >
                <Icon d={b.icon} size={16} className="dl-toolbar-icon" />
                <span className="dl-toolbar-label">{b.label}</span>
                {b.badge !== undefined && b.badge > 0 && (
                  <span
                    style={{
                      position: "absolute",
                      top: 2,
                      right: 2,
                      background: "var(--accent, var(--accent))",
                      color: "#fff",
                      borderRadius: "9px",
                      fontSize: "9px",
                      lineHeight: 1,
                      padding: "1px 4px",
                      pointerEvents: "none",
                      fontWeight: 700,
                    }}
                  >
                    {b.badge}
                  </span>
                )}
              </button>
            ))}
          </div>
        </React.Fragment>
      ))}
    </div>
  );
}
