import React, { useEffect, useMemo, useState } from "react";
import { HostCell } from "../ui/DownloadCells";
import type { HistoryItem, HistoryStatus } from "../types";
import { fmtBytes } from "../types";
import { selectRange, toggleSelection } from "../../lib/rangeSelection";
import { Icon, ic } from "../icons";
import { StatusBadge } from "../ui/StatusBadge";
import { KindTile } from "../ui/KindTile";
import { partitionMultiPartItems, type MultiPartPackage } from "../../lib/multipart";
import { getTaskActivityCounts, getTaskTimeline, type TaskActivityCounts, type TaskTimeline } from "../../api";
import { ColumnCells, ColumnHeader, TableScroll, useColumnLayout, type ColumnLayout } from "../ui/ColumnHeader";
import { HISTORY_COLUMNS, HISTORY_COLUMNS_KEY } from "../ui/downloadColumns";
import { InspectButton, TaskTimelineView, fmtDuration, fmtWhen } from "../components/activity";

const STATUS_ACCENT: Record<HistoryStatus, string> = {
  completed: "var(--success)",
  failed: "var(--danger)",
  cancelled: "rgba(148,163,184,0.7)",
};

const FILTERS: Array<"all" | HistoryStatus> = ["all", "completed", "failed", "cancelled"];

type RowProps = {
  item: HistoryItem;
  selected: boolean;
  expanded: boolean;
  counts?: TaskActivityCounts[string];
  onToggle: () => void;
  onSelect: (event: React.MouseEvent) => void;
  onRetry?: (item: HistoryItem) => void;
  nested?: boolean;
  columns: ColumnLayout;
};

function ActivityChips({ counts }: { counts?: TaskActivityCounts[string] }) {
  if (!counts || (!counts.captchas && !counts.hops)) {
    return <span style={{ fontSize: "9.5px", color: "var(--ink-20)" }}>—</span>;
  }
  return (
    <span className="flex items-center gap-1">
      {counts.captchas > 0 && (
        <span
          className="inline-flex items-center gap-0.5 px-1.5 rounded"
          style={{
            fontSize: "9.5px",
            background: counts.captchas_failed ? "rgba(251,191,36,0.14)" : "rgba(52,211,153,0.12)",
            color: counts.captchas_failed ? "#fbbf24" : "#6ee7b7",
          }}
          title={`${counts.captchas} captcha${counts.captchas === 1 ? "" : "s"}${counts.captchas_failed ? `, ${counts.captchas_failed} failed or stalled` : ""}`}
        >
          <Icon d={ic.captchas} size={9} />
          {counts.captchas}
        </span>
      )}
      {counts.hops > 0 && (
        <span
          className="inline-flex items-center gap-0.5 px-1.5 rounded"
          style={{ fontSize: "9.5px", background: "rgba(96,165,250,0.12)", color: "#93c5fd" }}
          title={`${counts.hops} shortlink step${counts.hops === 1 ? "" : "s"}`}
        >
          <Icon d={ic.link} size={9} />
          {counts.hops}
        </span>
      )}
    </span>
  );
}

function TimelinePanel({ taskId, url }: { taskId: string; url: string }) {
  const [timeline, setTimeline] = useState<TaskTimeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    getTaskTimeline(taskId)
      .then((result) => { if (!cancelled) setTimeline(result); })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : String(err)); });
    return () => { cancelled = true; };
  }, [taskId]);
  return (
    <div
      className="mx-2 mb-2 mt-0.5 px-3 py-3 rounded-lg"
      style={{ background: "rgba(0,0,0,0.22)", border: "1px solid var(--line-05)" }}
      onClick={(event) => event.stopPropagation()}
    >
      <div className="flex items-center gap-2 mb-2">
        <span className="truncate flex-1" style={{ fontSize: "10px", color: "var(--ink-35)", fontFamily: "'JetBrains Mono',monospace" }} title={url}>
          {url}
        </span>
        <InspectButton url={/^https?:\/\//.test(url) ? url : null} label="Open the source page" />
      </div>
      {error ? (
        <p style={{ fontSize: "11px", color: "var(--danger)" }}>Could not load history: {error}</p>
      ) : timeline ? (
        <TaskTimelineView timeline={timeline} />
      ) : (
        <p style={{ fontSize: "11px", color: "var(--ink-35)" }}>Loading history…</p>
      )}
    </div>
  );
}

function HistoryRow({ item, selected, expanded, counts, onToggle, onSelect, onRetry, nested = false, columns }: RowProps) {
  return (
    <div
      className={`hist-card${selected ? " selected" : ""}${expanded ? " expanded" : ""}${nested ? " nested" : ""}`}
      style={{ "--hist-accent": STATUS_ACCENT[item.status] } as React.CSSProperties}
    >
      <div
        className="dt-row py-1.5 cursor-pointer group"
        role="row"
        aria-selected={selected}
        aria-expanded={expanded}
        onClick={(event) => {
          event.stopPropagation();
          onSelect(event);
        }}
        onDoubleClick={(event) => {
          event.stopPropagation();
          onToggle();
        }}
      >
        <ColumnCells
          layout={columns}
          cells={{
            expand: (
              <button
                type="button"
                aria-expanded={expanded}
                aria-label={expanded ? "Hide download history" : "Show download history"}
                title={expanded ? "Hide details" : "Show everything that happened to this download"}
                onClick={(event) => {
                  event.stopPropagation();
                  onToggle();
                }}
                className="flex items-center justify-center rounded hover:bg-white/10"
                style={{ width: 22, height: 22, color: expanded ? "var(--warning)" : "var(--ink-45)" }}
              >
                <Icon d={expanded ? ic.chevronDown : ic.chevronRight} size={13} />
              </button>
            ),
            name: (
              <div className="flex items-center gap-2 min-w-0" style={{ paddingLeft: nested ? 16 : 0 }}>
                <KindTile category={item.category} size={20} />
                <div className="min-w-0">
                  <p className="truncate" style={{ fontSize: "11px", fontWeight: 500, color: "var(--ink-85)" }} title={item.name}>
                    {item.name}
                  </p>
                  <p
                    className="truncate"
                    style={{ fontSize: "9px", color: item.status === "failed" && item.error ? "rgba(248,113,113,0.8)" : "var(--ink-25)", marginTop: "1px" }}
                    title={item.error || item.savedTo}
                  >
                    {item.status === "failed" && item.error ? item.error : item.savedTo}
                  </p>
                </div>
              </div>
            ),
            activity: <ActivityChips counts={counts} />,
            size: (
              <span style={{ fontSize: "9.5px", color: "var(--ink-45)", fontFamily: "var(--font-mono)", whiteSpace: "nowrap" }}>
                {fmtBytes(item.size)}
              </span>
            ),
            took: (
              <span style={{ fontSize: "9.5px", color: "var(--ink-40)", fontFamily: "var(--font-mono)", whiteSpace: "nowrap" }}>
                {fmtDuration(item.durationSeconds)}
              </span>
            ),
            status: <StatusBadge status={item.status} />,
            host: <HostCell url={item.url} />,
            folder: (
              <span className="truncate" style={{ fontSize: "9.5px", color: "var(--ink-35)" }} title={item.savedTo}>
                {item.savedTo}
              </span>
            ),
            finished: item.status === "failed" && onRetry ? (
              <button
                type="button"
                className="history-action"
                style={{ padding: "1px 6px", fontSize: "9.5px" }}
                onClick={(event) => {
                  event.stopPropagation();
                  onRetry(item);
                }}
              >
                Retry
              </button>
            ) : (
              <span
                style={{ fontSize: "9.5px", color: "var(--ink-35)", whiteSpace: "nowrap" }}
                title={item.finishedAt ? new Date(item.finishedAt * 1000).toLocaleString() : ""}
              >
                {fmtWhen(item.finishedAt)}
              </span>
            ),
          }}
        />
      </div>
      {expanded && <TimelinePanel taskId={item.id} url={item.url} />}
    </div>
  );
}

function PackageGroup({ pkg, children, statusSummary, columns }: {
  pkg: MultiPartPackage<HistoryItem>;
  children: React.ReactNode;
  statusSummary: string;
  columns: ColumnLayout;
}) {
  const [open, setOpen] = useState(false);
  const failed = pkg.items.some((item) => item.status === "failed");
  const totalSize = pkg.items.reduce((sum, item) => sum + (item.size || 0), 0);
  return (
    <div className={`hist-card package${open ? " expanded" : ""}`} style={{ "--hist-accent": failed ? "var(--danger)" : "var(--success)" } as React.CSSProperties}>
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          setOpen((value) => !value);
        }}
        aria-expanded={open}
        className="dt-row w-full py-1.5 text-left rounded-lg hover:bg-white/[0.03]"
      >
        <ColumnCells
          layout={columns}
          cells={{
            expand: (
              <span className="flex items-center justify-center" style={{ color: "var(--ink-45)" }}>
                <Icon d={open ? ic.chevronDown : ic.chevronRight} size={12} />
              </span>
            ),
            name: (
              <span className="flex items-center gap-2 min-w-0">
                <KindTile category="archives" size={20} />
                <span className="px-1.5 rounded" style={{ fontSize: "8.5px", fontWeight: 700, background: "rgba(192,132,252,0.15)", color: "#d8b4fe" }}>
                  {pkg.items.length} PARTS
                </span>
                <span className="truncate" style={{ fontSize: "11px", fontWeight: 600, color: "var(--ink-85)" }} title={pkg.name}>
                  {pkg.name}
                </span>
              </span>
            ),
            activity: <span style={{ fontSize: "9.5px", color: "var(--ink-30)" }}>{statusSummary}</span>,
            size: (
              <span style={{ fontSize: "9.5px", color: "var(--ink-45)", fontFamily: "var(--font-mono)" }}>{fmtBytes(totalSize)}</span>
            ),
          }}
        />
      </button>
      {open && <div className="pb-1">{children}</div>}
    </div>
  );
}

export function HistoryPage({
  items = [],
  selectedIds = [],
  onSelectionChange,
  onRemove,
  onRetry,
  onBulkAction,
  onClear,
}: {
  items?: HistoryItem[];
  selectedIds?: string[];
  onSelectionChange?: (ids: string[]) => void;
  onRemove?: (id: string | string[]) => void;
  onRetry?: (item: HistoryItem) => void;
  onBulkAction?: (action: "pause" | "resume" | "stop" | "restart", ids: string[]) => void;
  onClear?: () => void;
}) {
  const [filter, setFilter] = useState<"all" | HistoryStatus>("all");
  const [search, setSearch] = useState("");
  const [selectionAnchor, setSelectionAnchor] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<string[]>([]);
  const [counts, setCounts] = useState<TaskActivityCounts>({});
  const columns = useColumnLayout(HISTORY_COLUMNS_KEY, HISTORY_COLUMNS);

  const sorted = useMemo(
    () => [...items].sort((a, b) => (b.finishedAt || 0) - (a.finishedAt || 0)),
    [items]
  );
  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    return sorted.filter((h) => (filter === "all" || h.status === filter) && (!query || h.name.toLowerCase().includes(query)));
  }, [sorted, filter, search]);
  const { packages, standalone } = useMemo(() => partitionMultiPartItems(filtered), [filtered]);

  const idsKey = items.map((item) => item.id).join(",");
  useEffect(() => {
    if (!idsKey) {
      setCounts({});
      return;
    }
    let cancelled = false;
    getTaskActivityCounts(idsKey.split(","))
      .then((result) => { if (!cancelled) setCounts(result); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [idsKey]);

  const totals = useMemo(() => ({
    all: items.length,
    completed: items.filter((item) => item.status === "completed").length,
    failed: items.filter((item) => item.status === "failed").length,
    cancelled: items.filter((item) => item.status === "cancelled").length,
    bytes: items.filter((item) => item.status === "completed").reduce((sum, item) => sum + (item.size || 0), 0),
  }), [items]);

  const visibleOrder = useMemo(
    () => [...packages.flatMap((p) => p.items.map((i) => i.id)), ...standalone.map((i) => i.id)],
    [packages, standalone]
  );
  const selectRow = (event: React.MouseEvent, id: string) => {
    if (event.shiftKey && selectionAnchor) onSelectionChange?.(selectRange(visibleOrder, selectionAnchor, id));
    else if (event.ctrlKey || event.metaKey) onSelectionChange?.(toggleSelection(selectedIds, id));
    else onSelectionChange?.([id]);
    setSelectionAnchor(id);
  };
  const clearSelection = () => {
    onSelectionChange?.([]);
    setSelectionAnchor(null);
  };
  const toggleExpanded = (id: string) =>
    setExpandedIds((ids) => (ids.includes(id) ? ids.filter((value) => value !== id) : [...ids, id]));

  const renderRow = (item: HistoryItem, nested = false) => (
    <HistoryRow
      key={item.id}
      item={item}
      nested={nested}
      selected={selectedIds.includes(item.id)}
      expanded={expandedIds.includes(item.id)}
      counts={counts[item.id]}
      onToggle={() => toggleExpanded(item.id)}
      onSelect={(event) => selectRow(event, item.id)}
      onRetry={onRetry}
      columns={columns}
    />
  );

  const filterColor: Record<"all" | HistoryStatus, string> = {
    all: "var(--accent-light)",
    completed: STATUS_ACCENT.completed,
    failed: STATUS_ACCENT.failed,
    cancelled: "#94a3b8",
  };

  return (
    <div className="flex flex-col h-full min-h-0">
      <div
        className="flex items-center gap-3 pl-5 pr-4 py-2.5 shrink-0"
        style={{ borderBottom: "1px solid var(--line-07)" }}
      >
        <div className="flex items-center gap-2.5">
          <h1 style={{ fontSize: "12.5px", fontWeight: 700, color: "var(--ink-90)" }}>History</h1>
          <div className="flex items-center gap-1">
            {FILTERS.map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className="px-2 py-0.5 rounded-md capitalize transition-all flex items-center gap-1 cursor-pointer"
                style={{
                  fontSize: "10.5px",
                  fontWeight: 600,
                  background: filter === f ? "rgba(255,255,255,0.08)" : "transparent",
                  color: filter === f ? "rgba(255,255,255,0.95)" : "rgba(255,255,255,0.4)",
                  border: filter === f ? `1px solid ${filterColor[f]}44` : "1px solid transparent",
                }}
              >
                {f !== "all" && <span className="w-1.5 h-1.5 rounded-full" style={{ background: filterColor[f] }} />}
                {f === "all" ? "All" : f}
                <span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "9.5px", color: "var(--ink-35)" }}>
                  {totals[f]}
                </span>
              </button>
            ))}
          </div>
        </div>

        <div className="ml-auto flex items-center gap-3">
          <span style={{ fontSize: "10px", color: "var(--ink-35)", fontFamily: "'JetBrains Mono',monospace" }}>
            {totals.completed} finished · {fmtBytes(totals.bytes)}
          </span>
          <div className="relative">
            <div className="absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" style={{ color: "var(--ink-30)" }}>
              <Icon d={ic.search} size={11} />
            </div>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search history…"
              className="win-input"
              style={{ paddingLeft: "26px", width: "168px", paddingTop: "3px", paddingBottom: "3px", fontSize: "11px" }}
            />
          </div>
        </div>
      </div>
      <TableScroll
        layout={columns}
        header={<ColumnHeader layout={columns} />}
        className="px-3 pb-1.5 space-y-0.5"
        role="grid"
        tabIndex={0}
        aria-label="Download history"
        aria-multiselectable="true"
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
            e.preventDefault();
            onSelectionChange?.(visibleOrder);
          }
        }}
        onClick={clearSelection}
      >
        {filtered.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 gap-2" style={{ color: "var(--ink-30)" }}>
            <Icon d={ic.history} size={28} />
            <p style={{ fontSize: "11.5px" }}>
              {items.length === 0 ? "Finished, failed and cancelled downloads will show up here." : "Nothing matches this filter."}
            </p>
          </div>
        )}
        {packages.map((pkg) => {
          const done = pkg.items.filter((item) => item.status === "completed").length;
          return (
            <PackageGroup key={pkg.key} pkg={pkg} statusSummary={`${done}/${pkg.items.length} done`} columns={columns}>
              {pkg.items.map((item) => renderRow(item, true))}
            </PackageGroup>
          );
        })}
        {standalone.map((item) => renderRow(item))}
      </TableScroll>
      {selectedIds.length > 0 && (
        <div className="dl-select-bar">
          <span style={{ fontSize: "10.5px", fontWeight: 600, color: "var(--ink-70)" }}>
            {selectedIds.length} selected
          </span>
          <button type="button" onClick={() => onRemove?.(selectedIds)} className="history-action danger">
            Delete
          </button>
          <button
            type="button"
            onClick={() => {
              const failedItems = selectedIds
                .map((id) => items.find((item) => item.id === id))
                .filter((item): item is HistoryItem => Boolean(item && item.status === "failed"));
              if (onBulkAction && failedItems.length > 0) {
                onBulkAction("restart", failedItems.map((item) => item.id));
              } else {
                failedItems.forEach((item) => onRetry?.(item));
              }
            }}
            className="history-action"
          >
            Retry failed
          </button>
          <button type="button" onClick={onClear} className="history-action">
            Clear completed
          </button>
          <button type="button" onClick={clearSelection} className="history-clear" aria-label="Clear selection">
            ×
          </button>
        </div>
      )}
    </div>
  );
}
