import React, { useState, useEffect, useMemo } from "react";
import type { DownloadWithHistory, Page } from "../types";
import {
  captchaBannerCopy,
  captchaTypeLabel,
  collectPendingChallenges,
  downloadBucket,
  isGenuinelyDownloading,
  packageBucket,
} from "../types";
import { ColumnHeader, TableScroll, useColumnLayout } from "../ui/ColumnHeader";
import { DOWNLOAD_COLUMNS, DOWNLOAD_COLUMNS_KEY } from "../ui/downloadColumns";
import { selectRange, toggleSelection } from "../../lib/rangeSelection";
import { renameTask, openPath, type CaptchaChallenge } from "../../api";
import { Icon, ic } from "../icons";
import { GroupHeader } from "../ui/GroupHeader";
import { ContextMenu } from "../components/ContextMenu";
import { DownloadSheet } from "../components/DownloadSheet";
import { DownloadPackageRow } from "../components/DownloadPackageRow";
import { DownloadRow } from "../components/DownloadRow";
import { DownloadsStatusBar } from "../components/DownloadsStatusBar";
import { DeleteConfirmModal } from "../components/DeleteConfirmModal";
import { detectMultiPart, normalizePackageName, partitionMultiPartItems } from "../../lib/multipart";

export function DownloadsPage({
  rowSize = "medium",
  showSpeeds = true,
  defaultSavePath,
  downloads = [],
  sheetId = null,
  setSheetId,
  focusedId = null,
  onFocusChange,
  selectedIds = [],
  onSelectionChange,
  onTaskAction,
  onBulkAction,
  onRemove,
  onAddUrl,
  onOpenSettings,
  onPrioritize,
  onPickDirectory,
  onOpenFolder,
  onNavigate,
  onOpenAdd,
  captchaPending = [],
  autoExtractMultipartDefault = true,
  captchaShowAutoBanner = true,
  captchaAutoSolve = false,
  onCaptchaAutoSolveChange,
  onCaptchaSolve,
}: {
  rowSize?: "small" | "medium" | "large";
  defaultSavePath: string;
  showSpeeds?: boolean;
  downloads?: DownloadWithHistory[];
  sheetId?: string | null;
  setSheetId: React.Dispatch<React.SetStateAction<string | null>>;
  focusedId?: string | null;
  onFocusChange?: (id: string) => void;
  selectedIds?: string[];
  onSelectionChange?: (ids: string[]) => void;
  onOpenSettings?: () => void;
  onPrioritize?: (id: string) => void;
  onPickDirectory?: () => Promise<string | null>;
  onOpenFolder?: (path: string) => void;
  onNavigate?: (page: Page) => void;
  onTaskAction?: (task: DownloadWithHistory, action: "pause" | "resume" | "stop" | "restart") => void;
  onBulkAction?: (action: "pause" | "resume" | "stop" | "restart", ids: string[]) => void;
  onRemove?: (id: string | string[], deleteFiles?: boolean) => void;
  onAddUrl?: (
    url: string,
    destination?: string,
    selectedItemIds?: string[],
    duplicateStrategy?: "skip" | "overwrite" | "rename" | "prompt",
    displayName?: string,
    autoExtract?: boolean
  ) => void;
  onOpenAdd?: () => void;
  captchaPending?: CaptchaChallenge[];
  autoExtractMultipartDefault?: boolean;
  captchaShowAutoBanner?: boolean;
  captchaAutoSolve?: boolean;
  onCaptchaAutoSolveChange?: (enabled: boolean) => void;
  onCaptchaSolve?: (id: string, solution: Record<string, unknown>, generation?: number) => Promise<void>;
}) {
  const [search, setSearch] = useState("");
  const [solvingChallenged, setSolvingChallenged] = useState(false);
  const [selectionAnchor, setSelectionAnchor] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  // Package expansion lives here, not in the row. A package moves between the
  // status groups as its parts progress, which remounts the row and would
  // otherwise snap an open package shut every time a status changed.
  const [pkgCollapsed, setPkgCollapsed] = useState<Record<string, boolean>>({});
  const togglePkgCollapsed = (key: string) =>
    setPkgCollapsed((prev) => ({ ...prev, [key]: !(prev[key] ?? true) }));
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; d: DownloadWithHistory; packageTaskIds?: string[] } | null>(null);
  const [renameState, setRenameState] = useState<{ id: string; value: string } | null>(null);
  const [deletePrompt, setDeletePrompt] = useState<{ ids: string[]; names: string[] } | null>(null);
  const sheet = sheetId ? downloads.find((x) => x.id === sheetId) ?? null : null;

  const focusRow = (id: string) => {
    setSelectionAnchor(id);
    if (focusedId === id) setSheetId(id);
    else {
      onFocusChange?.(id);
      setSheetId(null);
    }
  };
  const openSheet = (id: string) => {
    setSelectionAnchor(id);
    onFocusChange?.(id);
    setSheetId(id);
  };
  // Resizing, reordering, hiding and auto-fitting all live in the shared
  // column system; the header, rows and package parts share one template.
  const columns = useColumnLayout(DOWNLOAD_COLUMNS_KEY, DOWNLOAD_COLUMNS);
  const [sortKey, setSortKey] = useState<"name" | "size" | "speed" | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const toggleSort = (k: "name" | "size" | "speed") => {
    if (sortKey === k) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(k);
      setSortDir("asc");
    }
  };

  const selected = useMemo(() => new Set(selectedIds), [selectedIds]);
  const viewDownloads = useMemo(
    () => downloads.map((d) => ({ ...d, selected: selected.has(d.id) })),
    [downloads, selected]
  );
  const anySelected = selectedIds.length > 0;
  const selectedCount = selectedIds.length;

  const taskById = (id: string) => viewDownloads.find((d) => d.id === id);

  const needsUserTasks = useMemo(
    () => viewDownloads.filter((d) => d.status === "needs_user" || d.rawState === "needs_user"),
    [viewDownloads]
  );
  // Engine truth only: real pending challenges, deduped so shared package
  // challenges are not counted once per sibling part.
  const pendingChallenges = useMemo(
    () => collectPendingChallenges(captchaPending, viewDownloads),
    [captchaPending, viewDownloads]
  );
  const bannerChallenge = pendingChallenges[0] ?? null;
  const verificationCount = pendingChallenges.length;
  // Derive solving state from task challenge metadata (engine is source of truth)
  const solvingEngine = useMemo(() => {
    for (const task of needsUserTasks) {
      const challenge = (task.user_challenge || {}) as Record<string, unknown>;
      if (challenge.solver_active) return (challenge.solver_engine as string) || "Automated Solver";
      if (challenge.solution_received) return "__solved__";
    }
    return null;
  }, [needsUserTasks]);
  // The single CAPTCHA action: the button and the auto-solve checkbox both land
  // here, routed through the shell's onCaptchaSolve so engine state stays authoritative.
  const solveBannerChallenge = () => {
    if (bannerChallenge && !solvingEngine) void onCaptchaSolve?.(bannerChallenge.id, {});
  };
  const toggleDl = (id: string) => {
    const task = taskById(id);
    if (!task) return;
    const action =
      task.status === "downloading"
        ? "pause"
        : task.status === "paused"
        ? "resume"
        : task.status === "error" || task.status === "cancelled"
        ? "restart"
        : "resume";
    onTaskAction?.(task, action);
  };
  const promptDelete = (ids: string[]) => {
    if (!ids.length) return;
    const names = ids
      .map((id) => downloads.find((d) => d.id === id)?.name || "download")
      .filter(Boolean);
    setDeletePrompt({ ids, names });
  };

  const confirmDelete = (deleteFilesFromDisk: boolean) => {
    if (!deletePrompt) return;
    const { ids } = deletePrompt;
    if (ids.length === 1) {
      onRemove?.(ids[0], deleteFilesFromDisk);
    } else if (ids.length > 1) {
      onRemove?.(ids, deleteFilesFromDisk);
    }
    onSelectionChange?.(selectedIds.filter((id) => !ids.includes(id)));
    setDeletePrompt(null);
  };

  const removeDl = (id: string) => {
    promptDelete([id]);
  };
  const bulkPause = () => onBulkAction?.("pause", selectedIds);
  const bulkResume = () => onBulkAction?.("resume", selectedIds);
  const bulkRemove = () => {
    promptDelete(selectedIds);
  };
  const clearSelect = () => {
    onSelectionChange?.([]);
    setSelectionAnchor(null);
  };
  const toggleCollapse = (g: string) => setCollapsed((p) => ({ ...p, [g]: !p[g] }));

  // Listen for Delete and Backspace keyboard shortcuts across selected items
  useEffect(() => {
    const handleGlobalKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Delete" && e.key !== "Backspace") return;
      // Do not trigger if typing inside input, textarea, or contentEditable
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (selectedIds.length > 0) {
        e.preventDefault();
        promptDelete(selectedIds);
      }
    };

    window.addEventListener("keydown", handleGlobalKeyDown);
    return () => window.removeEventListener("keydown", handleGlobalKeyDown);
  }, [selectedIds, downloads]);

  const handleCtxAction = (action: string, id: string) => {
    // If context menu came from a package row or multi-selection, apply action to the whole set
    const targetIds =
      ctxMenu?.packageTaskIds && ctxMenu.packageTaskIds.length > 0
        ? ctxMenu.packageTaskIds
        : selectedIds.includes(id) && selectedIds.length > 1
        ? selectedIds
        : [id];

    switch (action) {
      case "open":
        openSheet(id);
        break;
      case "copy": {
        const urls = targetIds
          .map((tid) => downloads.find((x) => x.id === tid)?.url)
          .filter(Boolean);
        if (urls.length > 0) {
          navigator.clipboard.writeText(urls.join("\n")).catch(() => {});
        }
        break;
      }
      case "prioritize":
        targetIds.forEach((tid) => onPrioritize?.(tid));
        break;
      case "directory":
        onOpenSettings?.();
        break;
      case "folder": {
        const d = downloads.find((item) => item.id === id);
        if (d?.destination) onOpenFolder?.(d.destination);
        break;
      }
      case "toggle":
        if (targetIds.length > 1) {
          const anyRunning = targetIds.some(
            (tid) => taskById(tid)?.status === "downloading"
          );
          onBulkAction?.(anyRunning ? "pause" : "resume", targetIds);
        } else {
          toggleDl(id);
        }
        break;
      case "retry": {
        targetIds.forEach((tid) => {
          const task = taskById(tid);
          if (task) onTaskAction?.(task, "restart");
        });
        break;
      }
      case "rename": {
        const d = downloads.find((x) => x.id === id);
        if (d) setRenameState({ id, value: d.name });
        break;
      }
      case "remove":
        promptDelete(targetIds);
        break;
      default:
        break;
    }
  };

  /** Commit an in-progress rename to the engine. */
  const commitRename = async (id: string, newName: string) => {
    const trimmed = newName.trim();
    const task = downloads.find((x) => x.id === id);
    if (!trimmed || !task) { setRenameState(null); return; }
    try {
      await renameTask(id, trimmed, (task as { revision?: number }).revision ?? undefined);
    } catch {
      // Engine error shown in console; rename reverts visually on next snapshot
    } finally {
      setRenameState(null);
    }
  };

  /** Remove all-but-one duplicate tasks (same source fingerprint or URL+size). */
  const deduplicateDownloads = () => {
    const seen = new Map<string, string>(); // fingerprint → first id
    const dupIds: string[] = [];
    for (const d of downloads) {
      const key = (d as { source_fingerprint?: string }).source_fingerprint || `${d.url}|${d.size}`;
      if (seen.has(key)) {
        dupIds.push(d.id);
      } else {
        seen.set(key, d.id);
      }
    }
    if (dupIds.length === 1) {
      onRemove?.(dupIds[0]);
    } else if (dupIds.length > 1) {
      onRemove?.(dupIds);
    }
  };

  const active = viewDownloads.filter((d) => isGenuinelyDownloading(d));
  const totalSpeed = active.reduce((a, d) => a + d.speed, 0);
  const filtered = viewDownloads.filter((d) => d.name.toLowerCase().includes(search.toLowerCase()));

  // Grid column template — shared between header and every row
  // The action column must grow to fit Solve + Pause + Delete.  A fixed 40px
  // track lets the Solve button overflow on top of the other row actions.

  const sortItems = (items: DownloadWithHistory[]) => {
    if (!sortKey) return items;
    return [...items].sort((a, b) => {
      const av = sortKey === "name" ? a.name : sortKey === "size" ? a.size : a.speed;
      const bv = sortKey === "name" ? b.name : sortKey === "size" ? b.size : b.speed;
      if (typeof av === "string" && typeof bv === "string")
        return sortDir === "asc"
          ? av.localeCompare(bv, undefined, { sensitivity: "accent", numeric: true })
          : bv.localeCompare(av, undefined, { sensitivity: "accent", numeric: true });
      return sortDir === "asc" ? (av as number) - (bv as number) : (bv as number) - (av as number);
    });
  };

  const visibleOrder = useMemo(() => sortItems(filtered), [filtered, sortKey, sortDir]);
  const selectionOrder = useMemo(
    () =>
      [
        filtered.filter((item) => item.status === "downloading"),
        filtered.filter((item) => item.status === "paused" || item.status === "queued"),
        filtered.filter((item) => item.status === "completed"),
        filtered.filter((item) => item.status === "error" || item.status === "cancelled"),
      ].flatMap((items) => sortItems(items).map((item) => item.id)),
    [filtered, sortKey, sortDir]
  );
  const selectRow = (event: React.MouseEvent, id: string) => {
    if (event.shiftKey && selectionAnchor) onSelectionChange?.(selectRange(selectionOrder, selectionAnchor, id));
    else onSelectionChange?.(toggleSelection(selectedIds, id));
    setSelectionAnchor(id);
  };

  // Partition all filtered downloads into unified multi-part packages and standalone downloads
  const { multiPackagesMap, standaloneMap } = useMemo(() => {
    const { packages, standalone } = partitionMultiPartItems(filtered, sortItems);

    const pkgGroups: Record<string, typeof packages> = { transferring: [], waiting: [], completed: [], failed: [] };
    const stGroups: Record<string, typeof standalone> = { transferring: [], waiting: [], completed: [], failed: [] };

    for (const pkg of packages) {
      pkgGroups[packageBucket(pkg.items)].push(pkg);
    }

    for (const item of standalone) {
      stGroups[downloadBucket(item)].push(item);
    }

    return { multiPackagesMap: pkgGroups, standaloneMap: stGroups };
  }, [filtered, sortItems]);

  const groups = useMemo(() => {
    const groupDefs = [
      { key: "transferring", label: "Transferring", icon: ic.zap },
      { key: "waiting", label: "Waiting", icon: ic.pause },
      { key: "completed", label: "Completed", icon: ic.check },
      { key: "failed", label: "Failed", icon: ic.alertTriangle },
    ];

    return groupDefs.map((def) => {
      const pkgs = (multiPackagesMap[def.key] || []).map((pkg) => ({
        key: pkg.key,
        name: pkg.name,
        tasks: pkg.items,
      }));
      const standalone = standaloneMap[def.key] || [];
      const totalCount = pkgs.reduce((sum, p) => sum + p.tasks.length, 0) + standalone.length;

      let speed = 0;
      if (def.key === "transferring") {
        for (const p of pkgs) {
          for (const t of p.tasks) {
            if (t.status === "downloading") speed += t.speed;
          }
        }
        for (const t of standalone) {
          if (t.status === "downloading") speed += t.speed;
        }
      }

      return {
        key: def.key,
        label: def.label,
        icon: def.icon,
        packages: pkgs,
        standaloneItems: standalone,
        totalCount,
        groupSpeed: speed,
      };
    }).filter((g) => g.totalCount > 0);
  }, [multiPackagesMap, standaloneMap]);



  return (
    <>
      {deletePrompt && (
        <DeleteConfirmModal
          isOpen={true}
          itemNames={deletePrompt.names}
          onConfirm={confirmDelete}
          onCancel={() => setDeletePrompt(null)}
        />
      )}
      {ctxMenu && (
        <ContextMenu
          x={ctxMenu.x}
          y={ctxMenu.y}
          d={ctxMenu.d}
          packageCount={ctxMenu.packageTaskIds?.length}
          onClose={() => setCtxMenu(null)}
          onAction={handleCtxAction}
        />
      )}
      <div className="flex flex-col h-full min-h-0">
        {/* ── Header ─────────────────────────────────────────── */}
        <div
          className="flex items-center gap-3 pl-5 pr-4 py-2.5 shrink-0"
          style={{ borderBottom: "1px solid var(--line-07)" }}
        >
          <div className="flex-1 flex items-center gap-2">
            <h1 style={{ fontSize: "12.5px", fontWeight: 700, color: "var(--ink-90)" }}>Downloads</h1>
            {showSpeeds && active.length > 0 && (
              <span
                style={{
                  color: "var(--ink-30)",
                  fontSize: "9px",
                  fontWeight: 500,
                  letterSpacing: "0.02em",
                }}
              >
                {active.length} active
              </span>
            )}
            {showSpeeds && totalSpeed > 0 && (
              <span
                style={{
                  fontSize: "9.5px",
                  color: "var(--ink-30)",
                  fontFamily: "'JetBrains Mono',monospace",
                }}
              >
                ↓ {totalSpeed.toFixed(1)} MB/s
              </span>
            )}
          </div>
          <div className="relative">
            <div
              className="absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none"
              style={{ color: "var(--ink-25)" }}
            >
              <Icon d={ic.search} size={11} />
            </div>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter…"
              className="win-input"
              style={{
                paddingLeft: "26px",
                width: "118px",
                paddingTop: "4px",
                paddingBottom: "4px",
                fontSize: "11px",
              }}
            />
          </div>
        </div>

        {/* ── Captcha / Turnstile Action Required Banner ───────── */}
        {bannerChallenge && (!solvingEngine || captchaShowAutoBanner) && (
          <div
            className="mx-3 mt-2 mb-1 p-3 rounded-lg flex items-center justify-between gap-3 shrink-0"
            style={{
              background: "rgba(245, 158, 11, 0.12)",
              border: "1px solid rgba(245, 158, 11, 0.3)",
              boxShadow: "0 2px 10px rgba(0,0,0,0.2)",
            }}
          >
            <div className="flex items-center gap-2.5 min-w-0">
              <div
                className="w-7 h-7 rounded-md flex items-center justify-center shrink-0"
                style={{ background: "rgba(245, 158, 11, 0.2)", color: "#fbbf24" }}
              >
                <Icon d={ic.shield} size={15} />
              </div>
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span style={{ fontSize: "11.5px", fontWeight: 700, color: "#fbbf24" }}>
                    Verification Required ({verificationCount} pending)
                  </span>
                  <span
                    className="px-1.5 py-0.2 rounded uppercase"
                    style={{
                      fontSize: "9px",
                      fontFamily: "monospace",
                      background: "rgba(245, 158, 11, 0.25)",
                      color: "#fef3c7",
                    }}
                  >
                    {bannerChallenge.captcha_type || "verification"}
                  </span>
                </div>
                <p
                  style={{
                    fontSize: "10.5px",
                    color: "var(--ink-70)",
                    margin: 0,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={captchaTypeLabel(bannerChallenge.captcha_type)}
                >
                  {captchaBannerCopy(bannerChallenge.captcha_type)}
                </p>
                {solvingEngine === "__solved__" && (
                  <p
                    style={{
                      fontSize: "10.5px",
                      color: "var(--success)",
                      margin: 0,
                      fontWeight: 600,
                    }}
                  >
                    ✓ Solved! Resuming download…
                  </p>
                )}
                {solvingEngine && solvingEngine !== "__solved__" && (
                  <p
                    style={{
                      fontSize: "10.5px",
                      color: "#60a5fa",
                      margin: 0,
                      fontWeight: 600,
                      animation: "pulse 2s ease-in-out infinite",
                    }}
                  >
                    ⟳ Solving with {solvingEngine}…
                  </p>
                )}
              </div>
            </div>

            <div className="flex items-center gap-2 shrink-0">
              <button
                type="button"
                disabled={!!solvingEngine || !onCaptchaSolve}
                onClick={solveBannerChallenge}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-semibold transition-colors"
                style={{
                  background: solvingEngine ? "rgba(37,99,235,0.4)" : "#2563eb",
                  color: solvingEngine ? "rgba(255,255,255,0.45)" : "#ffffff",
                  cursor: solvingEngine || !onCaptchaSolve ? "not-allowed" : "pointer",
                }}
                title={solvingEngine ? "Solving in progress…" : "Trigger the engine CAPTCHA solver"}
              >
                <Icon d={ic.shield} size={12} />
                <span>{solvingEngine ? "Solving…" : "Solve CAPTCHA"}</span>
              </button>
              {onCaptchaAutoSolveChange && (
                <label
                  className="flex items-center gap-1.5 px-2 py-1.5 rounded text-[10.5px] font-medium cursor-pointer select-none"
                  style={{ color: captchaAutoSolve ? "#fde68a" : "rgba(255,255,255,0.7)" }}
                  title="Solve every future captcha automatically, without this banner"
                >
                  <input
                    type="checkbox"
                    checked={captchaAutoSolve}
                    onChange={(e) => {
                      const enabled = e.target.checked;
                      onCaptchaAutoSolveChange(enabled);
                      // Ticking the box shouldn't leave this challenge waiting for a click.
                      if (enabled) solveBannerChallenge();
                    }}
                    style={{ accentColor: "var(--warning)", width: 12, height: 12 }}
                  />
                  <span>Auto-solve all captchas in future</span>
                </label>
              )}
              <button
                type="button"
                onClick={() => onNavigate?.("captchas")}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-semibold transition-colors"
                style={{ background: "var(--surface-10)", color: "var(--ink-85)" }}
                onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-20)")}
                onMouseLeave={(e) => (e.currentTarget.style.background = "var(--surface-10)")}
              >
                <span>View Captchas ({verificationCount})</span>
              </button>
            </div>
          </div>
        )}

        {/* ── List: header pinned inside the one scroll container ── */}
        <TableScroll
          layout={columns}
          className="px-3 pb-2"
          role="grid"
          tabIndex={0}
          aria-label="Downloads"
          aria-multiselectable="true"
          style={{ paddingBottom: anySelected ? "58px" : "8px" }}
          header={
            <ColumnHeader
              layout={columns}
              sortKey={sortKey}
              sortDir={sortDir}
              onSort={(key) => toggleSort(key as "name" | "size" | "speed")}
            />
          }
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
              e.preventDefault();
              onSelectionChange?.(visibleOrder.map((item) => item.id));
            }
          }}
          onClick={() => {
            setSheetId(null);
            clearSelect();
          }}
        >
          {filtered.length === 0 && (
            <div
              className="flex flex-col items-center justify-center gap-3"
              style={{ color: "var(--ink-20)", minHeight: "160px", height: "100%" }}
            >
              <Icon d={ic.download} size={32} />
              <p style={{ fontSize: "12px", fontWeight: 500 }}>No downloads yet</p>
              <button
                onClick={() => onOpenAdd?.()}
                className="btn-accent px-4 py-1.5 rounded-md mt-1"
                style={{ fontSize: "11px", fontWeight: 600 }}
              >
                Add URL
              </button>
            </div>
          )}

          {groups.map((g) => {
            const isCollapsed = collapsed[g.key] ?? false;

            return (
              <div key={g.key} className="mb-2" role="rowgroup" aria-label={g.label}>
                <GroupHeader
                  icon={g.icon}
                  label={g.label}
                  count={g.totalCount}
                  speed={showSpeeds ? g.groupSpeed : 0}
                  collapsed={isCollapsed}
                  onToggle={() => toggleCollapse(g.key)}
                />
                {!isCollapsed && (
                  <div className="space-y-0.5 mt-0.5">
                    {/* Multi-part packages */}
                    {g.packages.map((pkg) => (
                      <DownloadPackageRow
                        key={pkg.key}
                        packageName={pkg.name}
                        tasks={pkg.tasks}
                        collapsed={pkgCollapsed[pkg.key] ?? true}
                        onToggleCollapsed={() => togglePkgCollapsed(pkg.key)}

                        selectedIds={selectedIds}
                        focusedId={focusedId}
                        onSelectRow={(e, id) => selectRow(e, id)}
                        onFocusRow={(id) => focusRow(id)}
                        onOpenSheet={(id) => openSheet(id)}
                        onTaskAction={onTaskAction}
                        onBulkAction={onBulkAction}
                        onRemoveTask={(id) => removeDl(id)}
                        onRemovePackage={(tasks) => promptDelete(tasks.map((t) => t.id))}
                        onSelectAllPackage={(tasks, select) => {
                          const pkgIds = tasks.map((t) => t.id);
                          if (select) {
                            onSelectionChange?.(Array.from(new Set([...selectedIds, ...pkgIds])));
                          } else {
                            onSelectionChange?.(selectedIds.filter((id) => !pkgIds.includes(id)));
                          }
                        }}
                        onContextMenu={(e, task, pkgIds) => {
                          e.preventDefault();
                          e.stopPropagation();
                          setCtxMenu({ x: e.clientX, y: e.clientY, d: task, packageTaskIds: pkgIds });
                        }}
                        showSpeeds={showSpeeds}
                        columns={columns}
                      />
                    ))}

                    {/* Standalone rows */}
                    {sortItems(g.standaloneItems).map((d) => (
                      <DownloadRow
                        key={d.id}
                        d={d}
                        size={rowSize}
                        columns={columns}
                        showSpeeds={showSpeeds}
                        focused={focusedId === d.id}
                        rename={
                          renameState?.id === d.id
                            ? {
                                value: renameState.value,
                                onChange: (value) => setRenameState({ id: d.id, value }),
                                onCommit: () => void commitRename(d.id, renameState.value),
                                onCancel: () => setRenameState(null),
                              }
                            : null
                        }
                        onClick={(e) => {
                          e.stopPropagation();
                          e.ctrlKey || e.metaKey || e.shiftKey ? selectRow(e, d.id) : focusRow(d.id);
                        }}
                        onDoubleClick={(e) => {
                          e.stopPropagation();
                          openSheet(d.id);
                        }}
                        onContextMenu={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          setCtxMenu({ x: e.clientX, y: e.clientY, d });
                        }}
                        onSelect={(e) => selectRow(e, d.id)}
                        onToggle={() => toggleDl(d.id)}
                        onRestart={() => onTaskAction?.(d, "restart")}
                        onRemove={() => removeDl(d.id)}
                      />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </TableScroll>
        <DownloadsStatusBar downloads={downloads} selectedCount={selectedIds.length} />

        {/* ── Detail sheet ─────────────────────────────────── */}
        <DownloadSheet
          d={sheet}
          defaultSavePath={defaultSavePath}
          onClose={() => setSheetId(null)}
          onOpenFolder={onOpenFolder}
          onNavigate={onNavigate}
        />

        {/* ── Batch action bar ───────────────────────────────── */}
        {anySelected && (
          <div className="dl-select-bar">
            <span
              style={{
                fontSize: "10.5px",
                fontWeight: 600,
                color: "var(--ink-70)",
                marginRight: "2px",
              }}
            >
              {selectedCount} selected
            </span>
            <div style={{ width: "1px", height: "14px", background: "var(--surface-10)", margin: "0 4px" }} />
            {[
              {
                label: "Pause",
                icon: ic.pause,
                onClick: bulkPause,
                color: "var(--ink-60)",
                bg: "rgba(255,255,255,0.07)",
              },
              {
                label: "Resume",
                icon: ic.play,
                onClick: bulkResume,
                color: "var(--ink-60)",
                bg: "rgba(255,255,255,0.07)",
              },
            ].map((b) => (
              <button
                key={b.label}
                onClick={b.onClick}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded transition-colors"
                style={{ fontSize: "10.5px", color: b.color, background: b.bg }}
                onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-12)")}
                onMouseLeave={(e) => (e.currentTarget.style.background = b.bg)}
              >
                <Icon d={b.icon} size={10} />
                {b.label}
              </button>
            ))}
            <button
              onClick={bulkRemove}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded transition-colors"
              style={{ fontSize: "10.5px", color: "var(--danger)", background: "rgba(239,68,68,0.1)" }}
              onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(239,68,68,0.22)")}
              onMouseLeave={(e) => (e.currentTarget.style.background = "rgba(239,68,68,0.1)")}
            >
              <Icon d={ic.trash} size={10} />
              Remove
            </button>
            <button
              onClick={clearSelect}
              style={{ color: "var(--ink-25)", marginLeft: "4px", fontSize: "14px", lineHeight: 1 }}
            >
              ×
            </button>
          </div>
        )}
      </div>
    </>
  );
}
