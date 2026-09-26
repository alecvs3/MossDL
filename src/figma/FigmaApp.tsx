import React, { useState, useEffect, useMemo, useRef } from "react";
import { ToastHost } from "./ui/Toasts";
import { UpdateBanner } from "./ui/UpdateBanner";
import { hostOfUrl } from "./ui/SiteIcon";
import { formatBytes } from "../lib/format";
import { AddUrlWindow } from "./components/AddUrlWindow";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { Icon, ic } from "./icons";
import { useHotkeys } from "./hooks/useHotkeys";
import { parseHotkeyMap } from "../lib/hotkeys";
import { splitPastedLinks } from "../lib/pageLinks";
import { setWindowMaterial } from "../api";
import { pruneSelection } from "../lib/rangeSelection";
import { nextZoom, DEFAULT_ZOOM } from "../lib/zoom";
import { windowAction } from "../api";
import { ProviderPresentationContext } from "./icons";
import { TitleBar } from "./components/TitleBar";
import { MenuBar } from "./components/MenuBar";
import { GlobalToolbar } from "./components/GlobalToolbar";
import { Sidebar } from "./components/Sidebar";
import { NowPlayingBar } from "./components/NowPlayingBar";
import { DevLogDrawer } from "./components/DevLogDrawer";
import { FigmaRouter } from "./components/FigmaRouter";
import { DesktopContextMenu } from "./components/DesktopContextMenu";
import { useFigmaShellState } from "./hooks/useFigmaShellState";
import type {
  Download,
  DownloadWithHistory,
  HistoryItem,
  Candidate,
  ModalData,
  AppSettings,
  FigmaAppProps,
  DownloadStatus,
  HistoryStatus,
  Page,
  SettingsTab,
  Provider,
} from "./types";

export type {
  Download,
  DownloadWithHistory,
  HistoryItem,
  Candidate,
  ModalData,
  AppSettings,
  FigmaAppProps,
  DownloadStatus,
  HistoryStatus,
  Page,
  SettingsTab,
  Provider,
};
export { fmtBytes, pct } from "./types";
export { Icon, ic, ProviderIcon, detectProvider, providerLabel, ProviderPresentationContext } from "./icons";

export default function FigmaApp(props: FigmaAppProps) {
  const {
    page,
    onPageChange,
    downloads,
    history = [],
    settings,
    defaultSavePath,
    onAddUrl,
    onTaskAction,
    onBulkAction,
    onRemoveTask,
    onClearCompleted,
    onSettingChange,
    onPrioritizeTask,
    onOpenFolder,
    providers = [],
    captureBatches = [],
    onDismissCaptureBatch,
    onOpenSetupWizard,
  } = props;
  const {
    localSettings,
    setLocalSettings,
    devLogOpen,
    setDevLogOpen,
    devLogInitialLevel,
    handleToggleDevLogs,
    setZoom,
    effectiveZoom,
    theme,
    themeStyle,
  } = useFigmaShellState(settings);

  const [sheetId, setSheetId] = useState<string | null>(null);
  const [focusedDownloadId, setFocusedDownloadId] = useState<string | null>(null);
  const [addWindowOpen, setAddWindowOpen] = useState(false);
  const [selectedDownloadIds, setSelectedDownloadIds] = useState<string[]>([]);
  const [selectedHistoryIds, setSelectedHistoryIds] = useState<string[]>([]);
  const [desktopCtxMenu, setDesktopCtxMenu] = useState<{ x: number; y: number } | null>(null);
  const [dismissedCaptureBatches, setDismissedCaptureBatches] = useState<Set<string>>(() => new Set());
  const [exploreUrl, setExploreUrl] = useState<string | null>(null);
  const [capturedIntake, setCapturedIntake] = useState<{ url: string; batchId: string } | null>(null);
  const autoOpenedBatchesRef = useRef<Set<string>>(new Set());

  const handleMainContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    const target = e.target as HTMLElement | null;
    if (
      !target ||
      target.closest(
        "[class*='dl-row'], [class*='dl-pkg-row'], .context-menu, button, input, select, textarea, a, [role='button'], [role='option'], [data-no-desktop-ctx]"
      )
    ) {
      setDesktopCtxMenu(null);
      return;
    }
    setDesktopCtxMenu({ x: e.clientX, y: e.clientY });
  };

  const handleSuppressContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    setDesktopCtxMenu(null);
  };

  const pendingCaptureBatch = useMemo(() => {
    return (captureBatches ?? []).find(
      (b) => b.state === "pending" && !dismissedCaptureBatches.has(b.batch_id) && b.candidates && b.candidates.length > 0
    );
  }, [captureBatches, dismissedCaptureBatches]);

  useEffect(() => {
    if (pendingCaptureBatch && pendingCaptureBatch.candidates?.[0]?.url) {
      const bId = pendingCaptureBatch.batch_id;
      if (!autoOpenedBatchesRef.current.has(bId)) {
        autoOpenedBatchesRef.current.add(bId);
        setCapturedIntake({
          url: pendingCaptureBatch.candidates[0].url,
          batchId: bId,
        });
      }
    }
  }, [pendingCaptureBatch]);

  useEffect(
    () => setSelectedDownloadIds((ids) => pruneSelection(ids, downloads.map((item) => item.id))),
    [downloads]
  );
  useEffect(() => {
    setFocusedDownloadId((id) => (id && downloads.some((item) => item.id === id) ? id : null));
  }, [downloads]);
  useEffect(
    () => setSelectedHistoryIds((ids) => pruneSelection(ids, history.map((item) => item.id))),
    [history]
  );

  const handleOpenSheet = (id: string) => {
    onPageChange("downloads");
    setFocusedDownloadId(id);
    setSheetId(id);
  };

  // Windows paints the backdrop; the UI only tints it. "window-glass" is set
  // once a material really applied, so an opaque fallback never goes see-through.
  useEffect(() => {
    let cancelled = false;
    setWindowMaterial(localSettings.acrylic ? "acrylic" : "mica")
      .then((applied) => {
        if (!cancelled) document.documentElement.classList.toggle("window-glass", applied !== "none");
      })
      .catch(() => {
        if (!cancelled) document.documentElement.classList.remove("window-glass");
      });
    return () => { cancelled = true; };
  }, [localSettings.acrylic]);

  // "Clear" removes finished downloads from the list; the files stay on disk.
  // It asks first unless the user turned the warning off.
  const [clearPrompt, setClearPrompt] = useState(false);
  const finishedCount = downloads.filter((download) => download.status === "completed").length;
  const requestClearCompleted = () => {
    if (!finishedCount) return;
    if (localSettings.confirmClear === false) {
      onClearCompleted?.();
      return;
    }
    setClearPrompt(true);
  };

  // The Add window opens over whatever page is showing.
  const requestAddUrl = () => {
    setCapturedIntake(null);
    setAddWindowOpen(true);
  };

  // Every shortcut in the app resolves through one map, so rebinding in
  // Settings takes effect everywhere at once.
  const hotkeyMap = useMemo(() => parseHotkeyMap(localSettings.hotkeys), [localSettings.hotkeys]);
  useHotkeys(hotkeyMap, {
    "add-url": requestAddUrl,
    "add-from-clipboard": () => {
      void navigator.clipboard
        ?.readText()
        .then((text) => {
          const links = splitPastedLinks(text || "");
          if (links.length) void onAddUrl?.(links[0]);
          else requestAddUrl();
        })
        .catch(() => requestAddUrl());
    },
    "focus-search": () => {
      const search = document.querySelector<HTMLInputElement>(
        'input[placeholder^="Filter"], input[placeholder^="Search"]'
      );
      search?.focus();
      search?.select();
    },
    settings: () => onPageChange("settings"),
    "pause-all": () => onBulkAction?.("pause", downloads.filter((d) => d.status === "downloading").map((d) => d.id)),
    "resume-all": () => onBulkAction?.("resume", downloads.filter((d) => d.status === "paused").map((d) => d.id)),
    "clear-finished": requestClearCompleted,
    // Only on Downloads: elsewhere Delete belongs to that page's own list.
    "delete-selected": () => { if (page === "downloads") selectedDownloadIds.forEach((id) => onRemoveTask?.(id)); },
    "go-downloads": () => onPageChange("downloads"),
    "go-grabber": () => onPageChange("explore"),
    "go-captchas": () => onPageChange("captchas"),
    "go-routes": () => onPageChange("routes"),
    "go-history": () => onPageChange("history"),
    "zoom-in": () => setZoom((value: number) => nextZoom(value, 10)),
    "zoom-out": () => setZoom((value: number) => nextZoom(value, -10)),
    "zoom-reset": () => setZoom(DEFAULT_ZOOM),
    "dev-logs": () => handleToggleDevLogs(),
  });

  /** Remove all-but-one duplicates from the engine download list. */
  const handleDeduplicate = () => {
    const seen = new Map<string, string>(); // key → first id kept
    for (const d of downloads) {
      const key = (d as { source_fingerprint?: string }).source_fingerprint || `${d.url}|${d.size}`;
      if (seen.has(key)) {
        onRemoveTask?.(d.id);
      } else {
        seen.set(key, d.id);
      }
    }
  };

  const handleQueueAssets = async (assets: { name: string; url: string; size: number; type: string }[]) => {
    onPageChange("downloads");
    for (const asset of assets) {
      await onAddUrl?.(asset.url, undefined, undefined, undefined, asset.name, true);
    }
  };

  const changeSetting = (tab: SettingsTab, key: string, value: string | boolean) => {
    setLocalSettings((current: AppSettings) => ({ ...current, [key]: value }));
    onSettingChange?.(tab, key, value);
  };
  const menuProps = {
    setPage: onPageChange,
    onAddUrl: requestAddUrl,
    onPauseAll: () =>
      onBulkAction?.(
        "pause",
        downloads.filter((d) => d.status === "downloading").map((d) => d.id)
      ),
    onResumeAll: () =>
      onBulkAction?.(
        "resume",
        downloads.filter((d) => d.status === "paused").map((d) => d.id)
      ),
    pauseAllEnabled: downloads.some((d) => d.status === "downloading"),
    resumeAllEnabled: downloads.some((d) => d.status === "paused"),
    onClearCompleted: finishedCount ? requestClearCompleted : undefined,
    onToggleClipboard: () => changeSetting("general", "clipboardWatcher", !localSettings.clipboardWatcher),
    clipboardWatcherEnabled: localSettings.clipboardWatcher,
    onZoomIn: () => setZoom((value: number) => nextZoom(value, 10)),
    onZoomOut: () => setZoom((value: number) => nextZoom(value, -10)),
    onZoomReset: () => setZoom(DEFAULT_ZOOM),
    onToggleDevLogs: () => handleToggleDevLogs(),
    onOpenSetupWizard: onOpenSetupWizard,
    onQuit: () =>
      void windowAction("destroy").catch((error) =>
        console.error("Could not quit the application", error)
      ),
  };
  const downloadViews = useMemo(
    () => downloads.map((item) => ({ ...item, selected: selectedDownloadIds.includes(item.id) })),
    [downloads, selectedDownloadIds]
  );

  // Derive whether a captcha solve is in progress — engine is source of truth
  const solvingCaptcha = useMemo(() => {
    const needsUserTask = downloads.find(
      (d) => d.status === "needs_user" || (d as any).rawState === "needs_user"
    );
    if (!needsUserTask) return false;
    const challenge = (needsUserTask as any).user_challenge;
    return !!(challenge?.solver_active);
  }, [downloads]);

  return (
    <ProviderPresentationContext.Provider value={providers}>
      <div
        className={`figma-shell flex flex-col h-full min-h-0 overflow-hidden${
          localSettings.acrylic ? " acrylic-enabled" : ""
        }`}
        data-theme={theme}
        style={themeStyle}
      >
        {/* mica noise layer */}
        <div
          className="absolute inset-0 pointer-events-none"
          style={{
            opacity: 0.04,
            backgroundImage:
              'url("data:image/svg+xml,%3Csvg viewBox=\'0 0 512 512\' xmlns=\'http://www.w3.org/2000/svg\'%3E%3Cfilter id=\'n\'%3E%3CfeTurbulence type=\'fractalNoise\' baseFrequency=\'0.75\' numOctaves=\'4\' stitchTiles=\'stitch\'/%3E%3C/filter%3E%3Crect width=\'100%25\' height=\'100%25\' filter=\'url(%23n)\'/%3E%3C/svg%3E")',
            backgroundSize: "256px",
          }}
        />
        {/* The title bar's menus drop down over everything below, so its wrapper sits above them. */}
        <div className="shell-chrome-top" onContextMenu={handleSuppressContextMenu}>
          <TitleBar
            systemTray={localSettings.systemTray}
            onToggleDevLogs={handleToggleDevLogs}
            isDevLogsOpen={devLogOpen}
          >
            <MenuBar {...menuProps} />
          </TitleBar>
        </div>
        <UpdateBanner />
        <div className="shell-chrome-toolbar" onContextMenu={handleSuppressContextMenu}>
          <GlobalToolbar
            downloads={downloadViews}
            selectedIds={selectedDownloadIds}
            onTaskAction={onTaskAction}
            onBulkAction={onBulkAction}
            onRemove={onRemoveTask}
            onClearCompleted={requestClearCompleted}
            onAddUrl={requestAddUrl}
            onOpenFolder={onOpenFolder}
            onDeduplicate={handleDeduplicate}
          />
        </div>
        <div className="flex flex-1 min-h-0 relative">
          <div onContextMenu={handleSuppressContextMenu} className="shrink-0 flex">
            <Sidebar
              page={page}
              setPage={onPageChange}
              captchaCount={(props.captchaPending ?? []).length}
              captureCount={(captureBatches ?? []).filter((b) => b.state === "pending" && !dismissedCaptureBatches.has(b.batch_id)).length}
            />
          </div>
          <main
            className="flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden"
            onContextMenu={handleMainContextMenu}
          >
            {pendingCaptureBatch && !capturedIntake && page !== "explore" && (
              <div
                className="shrink-0 flex items-center justify-between px-4 py-2 text-xs font-medium"
                style={{
                  background: "rgba(14, 165, 233, 0.12)",
                  borderBottom: "1px solid rgba(14, 165, 233, 0.25)",
                  color: "#38bdf8",
                }}
              >
                <div className="flex items-center gap-2 truncate">
                  <Icon d={ic.sparkles} size={14} />
                  <span className="truncate">
                    Captured download from browser: <strong>{pendingCaptureBatch.candidates?.[0]?.filename || pendingCaptureBatch.candidates?.[0]?.url}</strong>
                    <span style={{ opacity: 0.75 }}>
                      {pendingCaptureBatch.candidates?.[0]?.size ? ` · ${formatBytes(pendingCaptureBatch.candidates[0].size)}` : ""}
                      {hostOfUrl(pendingCaptureBatch.page_url || pendingCaptureBatch.page_origin) ? ` · from ${hostOfUrl(pendingCaptureBatch.page_url || pendingCaptureBatch.page_origin)}` : ""}
                      {pendingCaptureBatch.candidates.length > 1 ? ` · +${pendingCaptureBatch.candidates.length - 1} more` : ""}
                    </span>
                  </span>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    type="button"
                    className="px-2.5 py-1 rounded text-xs font-semibold text-white shadow-sm transition-all"
                    style={{ background: "rgba(14, 165, 233, 0.3)", border: "1px solid rgba(14, 165, 233, 0.45)" }}
                    onClick={() =>
                      pendingCaptureBatch.candidates?.[0]?.url &&
                      setCapturedIntake({
                        url: pendingCaptureBatch.candidates[0].url,
                        batchId: pendingCaptureBatch.batch_id,
                      })
                    }
                  >
                    Review &amp; Download
                  </button>
                  <button
                    type="button"
                    className="px-2.5 py-1 rounded text-xs font-semibold transition-all"
                    style={{ color: "#bae6fd", border: "1px solid rgba(14, 165, 233, 0.35)" }}
                    onClick={() => onPageChange("explore")}
                    title="See everything the browser captured on this page"
                  >
                    Open in Explore
                  </button>
                  <button
                    type="button"
                    className="px-2 py-1 rounded text-xs opacity-70 hover:opacity-100 transition-opacity"
                    onClick={() => {
                      setDismissedCaptureBatches((prev) => new Set([...prev, pendingCaptureBatch.batch_id]));
                      onDismissCaptureBatch?.(pendingCaptureBatch.batch_id);
                    }}
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            )}
            <div className="flex-1 min-h-0 overflow-hidden flex flex-col" style={{ zoom: effectiveZoom / 100 }}>
              <FigmaRouter
                {...props}
                page={page}
                onPageChange={onPageChange}
                settings={localSettings}
                defaultSavePath={defaultSavePath}
                downloadViews={downloadViews}
                selectedDownloadIds={selectedDownloadIds}
                setSelectedDownloadIds={setSelectedDownloadIds}
                sheetId={sheetId}
                setSheetId={setSheetId}
                focusedDownloadId={focusedDownloadId}
                setFocusedDownloadId={setFocusedDownloadId}
                openAddWindow={requestAddUrl}
                handleQueueAssets={handleQueueAssets}
                selectedHistoryIds={selectedHistoryIds}
                setSelectedHistoryIds={setSelectedHistoryIds}
                changeSetting={changeSetting}
                openSettings={() => onPageChange("settings")}
                exploreUrl={exploreUrl}
              />
            </div>
            <DevLogDrawer
              isOpen={devLogOpen}
              onClose={() => setDevLogOpen(false)}
              initialLevel={devLogInitialLevel}
            />
            {/* The detail sheet occupies the same strip; showing both stacks
                two bars over each other. The sheet wins while it is open. */}
            {!sheetId && (
              <NowPlayingBar
                downloads={downloads}
                onTaskAction={onTaskAction}
                onOpenSheet={handleOpenSheet}
                statusText={solvingCaptcha ? "Solving captcha(s)…" : undefined}
              />
            )}
            {desktopCtxMenu && (
              <DesktopContextMenu
                x={desktopCtxMenu.x}
                y={desktopCtxMenu.y}
                currentPage={page}
                onClose={() => setDesktopCtxMenu(null)}
                onAddUrl={requestAddUrl}
                onPasteUrl={async () => {
                  try {
                    const text = await navigator.clipboard.readText();
                    if (text.trim()) requestAddUrl();
                  } catch {
                    /* clipboard denied */
                  }
                }}
                onOpenFolder={onOpenFolder ? () => onOpenFolder(defaultSavePath) : undefined}
                onNavigatePage={onPageChange}
                onSelectAll={
                  page === "downloads"
                    ? () => setSelectedDownloadIds(downloadViews.map((d) => d.id))
                    : page === "history"
                    ? () => setSelectedHistoryIds(history.map((h: HistoryItem) => h.id))
                    : undefined
                }
                onClearCompleted={
                  page === "downloads" && downloadViews.some((d) => d.status === "completed")
                    ? requestClearCompleted
                    : undefined
                }
              />
            )}
          </main>
        </div>
      </div>
      <ConfirmDialog
        open={clearPrompt}
        title={`Clear ${finishedCount} finished download${finishedCount === 1 ? "" : "s"}?`}
        body={
          <>
            This removes finished downloads from the list so it stays tidy. The
            downloaded files stay where they are on disk, and unfinished
            downloads are not touched.
          </>
        }
        confirmLabel="Clear list"
        confirmIcon={ic.clearDone}
        rememberLabel="Don't ask me again"
        onCancel={() => setClearPrompt(false)}
        onConfirm={(remember) => {
          if (remember) changeSetting("general", "confirmClear", false);
          setClearPrompt(false);
          onClearCompleted?.();
        }}
      />
      <AddUrlWindow
        open={addWindowOpen || !!capturedIntake}
        onClose={() => {
          setAddWindowOpen(false);
          setCapturedIntake(null);
        }}
        initialUrl={capturedIntake?.url}
        lockedUrl={!!capturedIntake}
        captureBatchId={capturedIntake?.batchId}
        onDismissCaptured={() => {
          if (capturedIntake) {
            setDismissedCaptureBatches((prev) => new Set([...prev, capturedIntake.batchId]));
            onDismissCaptureBatch?.(capturedIntake.batchId);
            setCapturedIntake(null);
          }
        }}
        onAdd={(url, destination, selectedItemIds, duplicateStrategy, displayName, autoExtract, shouldRefresh) =>
          onAddUrl?.(url, destination, selectedItemIds, duplicateStrategy, displayName, autoExtract, shouldRefresh)
        }
        onAdded={() => onPageChange("downloads")}
        onExplore={(url) => { setExploreUrl(url); onPageChange("explore"); }}
        defaultSavePath={defaultSavePath}
        onPickDirectory={props.onPickDirectory}
        autoExtractDefault={props.autoExtractMultipartDefault ?? true}
      />
      <ToastHost />
    </ProviderPresentationContext.Provider>
  );
}
