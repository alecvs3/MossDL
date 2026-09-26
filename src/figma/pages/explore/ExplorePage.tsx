// Explore: one place for everything that finds downloads — pages you explore,
// links you paste, and what the browser extension captures — shown as one
// sortable tree. Replaces the separate Grabber and Capture pages.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CaptureBatch, CaptureCandidate, CaptureImportResult, LinkGrabberEntry, MediaPlan, Task } from "../../../api";
import MediaCaptureOverlay, { isEligibleMediaCandidate } from "../../../components/MediaCaptureOverlay";
import { extractLinks, openPath } from "../../../api";
import { loadRecentUrls, rememberUrl } from "./recentUrls";
import type { DownloadWithHistory } from "../../types";
import { formatBytes } from "../../../lib/format";
import { Icon, ic, detectProvider } from "../../icons";
import { ColumnHeader, TableScroll, useColumnLayout } from "../../ui/ColumnHeader";
import { EXPLORE_COLUMNS, EXPLORE_COLUMNS_KEY } from "../../ui/downloadColumns";
import { ActionMenu, type MenuEntry } from "../../ui/ActionMenu";
import { toast, toastUndo } from "../../ui/Toasts";
import { copyText } from "../../ui/copyText";
import { buildTree, siteOf, walk, type ExploreNode } from "./exploreModel";
import {
  checkState, facetCounts, filesUnder, initialView, isDownloadable, isOpen, selectedFiles, setChecked, summarize, visibleRows,
  type SortKey, type ViewState,
} from "./exploreView";
import { exploreMenu, type MenuActions } from "./exploreMenus";
import { useExploreData } from "./useExploreData";
import { ExploreRow } from "./ExploreRow";
import { ExploreFilters } from "./ExploreFilters";
import { useExploreKeys } from "./useExploreKeys";
import { useFreshRows } from "./useFreshRows";
import { useExploreDownload, type DownloadDeps } from "./useExploreDownload";
import "./explore.css";

export interface ExplorePageProps extends DownloadDeps {
  downloads: DownloadWithHistory[];
  captureBatches: CaptureBatch[];
  linkEntries: LinkGrabberEntry[];
  initialUrl?: string | null;
  onDismissCaptureBatch?: (batchId: string) => void;
  onLinkAdd?: (text: string) => Promise<void>;
  onLinkBulkDelete?: (ids: string[]) => Promise<void>;
  onMediaPlan?: (url: string) => Promise<MediaPlan>;
  onMediaImport?: (candidate: CaptureCandidate, url: string) => Promise<CaptureImportResult | Task>;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
/** A direct file or file-host link rather than a page: judged by the path, never the host ("site.com" is a page). */
const looksLikeFile = (url: string) => {
  let path = "";
  try { path = new URL(url).pathname; } catch { return false; }
  return /\.[a-z0-9]{2,5}$/i.test(path) || detectProvider(url) !== "direct";
};

export function ExplorePage(props: ExplorePageProps) {
  const { downloads, captureBatches, linkEntries, defaultSavePath } = props;
  const data = useExploreData();
  const columns = useColumnLayout(EXPLORE_COLUMNS_KEY, EXPLORE_COLUMNS);
  const [view, setView] = useState<ViewState>(initialView);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [anchorId, setAnchorId] = useState<string | null>(null);
  const [url, setUrl] = useState(props.initialUrl ?? "");
  const [useBrowser, setUseBrowser] = useState(false);
  const [showFilters, setShowFilters] = useState(true);
  const [mediaFor, setMediaFor] = useState<CaptureCandidate | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuEntry[]; label: string } | null>(null);
  const urlRef = useRef<HTMLInputElement | null>(null);
  const queryRef = useRef<HTMLInputElement | null>(null);

  const tree = useMemo(() => buildTree({
    crawls: data.crawls, resolutions: data.resolutions, resolveErrors: data.resolveErrors, resolving: data.resolving,
    batches: captureBatches, entries: linkEntries,
  }), [data.crawls, data.resolutions, data.resolveErrors, data.resolving, captureBatches, linkEntries]);
  const rows = useMemo(() => visibleRows(tree, view), [tree, view]);
  const fresh = useFreshRows(tree);
  const counts = useMemo(() => facetCounts(tree, view), [tree, view]);
  const selection = useMemo(() => selectedFiles(tree, selectedIds, view), [tree, selectedIds, view]);
  const byId = useMemo(() => { const m = new Map<string, ExploreNode>(); walk(tree, (n) => m.set(n.id, n)); rows.forEach((r) => m.set(r.node.id, r.node)); return m; }, [tree, rows]);

  // "In Downloads" is the engine's task list, not a flag we keep ourselves.
  const queuedUrls = useMemo(() => new Set(downloads.map((d) => d.url)), [downloads]);
  const importedCaptures = useMemo(() => new Set(captureBatches.flatMap((b) => (b.state === "imported" ? [b.batch_id] : []))), [captureBatches]);
  const queued = useCallback((n: ExploreNode) =>
    Boolean(n.url && queuedUrls.has(n.url)) || (n.origin.kind === "capture" && importedCaptures.has(n.origin.batchId)), [queuedUrls, importedCaptures]);

  // A URL handed over from the Add URL window explores immediately.
  useEffect(() => {
    if (props.initialUrl) { setUrl(props.initialUrl); exploreUrl(props.initialUrl); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.initialUrl]);

  /* ── actions (all engine work goes through props / useExploreData) ── */
  const download = useExploreDownload(props);

  const copy = copyText;

  const toggleOpen = useCallback((n: ExploreNode, open?: boolean) =>
    setView((v) => ({ ...v, open: { ...v.open, [n.id]: open ?? !isOpen(n, v) } })), []);
  const openAll = useCallback((n: ExploreNode, open: boolean) =>
    setView((v) => { const next = { ...v.open }; walk([n], (x) => { if (x.children.length) next[x.id] = open; }); return { ...v, open: next }; }), []);

  // Drops rows for good: crawls from memory, captures dismissed, pasted links deleted.
  const discard = useCallback((nodes: ExploreNode[]) => {
    const entryIds: string[] = [];
    const collect = (list: ExploreNode[]) => {
      for (const n of list) {
        if (n.origin.kind === "crawl") data.removeCrawl(n.origin.crawlId);
        else if (n.batchIds) n.batchIds.forEach((id) => props.onDismissCaptureBatch?.(id));
        else if (n.origin.kind === "entry") entryIds.push(n.origin.entry.id);
        if (n.children.length) collect(n.children);
      }
    };
    collect(nodes);
    if (entryIds.length) void props.onLinkBulkDelete?.(entryIds);
  }, [data, props]);

  // Removal hides rows at once; the engine side (dismissing captures, deleting
  // pasted links, dropping crawls) is committed when the Undo window closes.
  const remove = useCallback((picked: ExploreNode[]) => {
    // A site header stands for all of its pages.
    const nodes = picked.flatMap((n) => (n.type === "site" ? n.children : [n]));
    const ids = new Set(nodes.map((n) => n.id));
    const commit = () => discard(nodes);
    setView((v) => ({ ...v, removed: new Set([...v.removed, ...ids]) }));
    setSelectedIds((s) => setChecked(s, nodes, false, view));
    toastUndo(`Removed ${nodes.length === 1 ? `“${nodes[0].label}”` : plural(nodes.length, "row")}`, commit,
      () => setView((v) => ({ ...v, removed: new Set([...v.removed].filter((id) => !ids.has(id))) })));
  }, [data, props, view]);

  const actions: MenuActions = {
    download: (nodes, opts) => void download(nodes, opts),
    follow: (n, headless) => void data.follow(n, headless),
    reexplore: (n, headless) => n.url && void data.explore(n.url, { headless }),
    copy,
    openExternal: (target) => void openPath(target).catch((error: unknown) => toast(`Couldn't open: ${String(error)}`, { tone: "danger" })),
    setChecked: (nodes, on) => setSelectedIds((s) => setChecked(s, nodes, on, view)),
    selectWhere: (pred) => { const hits: ExploreNode[] = []; walk(tree, (x) => { if (isDownloadable(x) && pred(x)) hits.push(x); }); setSelectedIds((s) => setChecked(s, hits, true, view)); },
    filterKind: (kind) => setView((v) => ({ ...v, kind })),
    filterHost: (host) => setView((v) => ({ ...v, hosts: v.hosts.includes(host) ? v.hosts : [...v.hosts, host] })),
    toggleOpen, openAll, remove,
    media: (n) => { if (n.origin.kind === "capture") setMediaFor(n.origin.candidate); },
  };
  const mediaEligible = (n: ExploreNode) =>
    Boolean(props.onMediaPlan && props.onMediaImport) && n.origin.kind === "capture" && isEligibleMediaCandidate(n.origin.candidate);

  const primary = (n: ExploreNode) => {
    if (isDownloadable(n)) void download([n]);
    else if (n.followable) void data.follow(n);
    else if (n.children.length) toggleOpen(n);
  };
  const showMenu = (n: ExploreNode, x: number, y: number) => {
    setFocusedId(n.id);
    setMenu({ x, y, label: `Actions for ${n.label}`, items: exploreMenu(n, {
      actions, open: isOpen(n, view), check: checkState(n, selectedIds, view), files: filesUnder(n, view), selection, queued, mediaEligible,
    }) });
  };

  const [recent, setRecent] = useState<string[]>(loadRecentUrls);
  // Exploring a new site clears the others from view (Undo brings them back).
  // Only the view changes: browser captures stay in the engine.
  const exploreUrl = (target: string) => {
    setRecent(rememberUrl(target));
    const others = tree.filter((r) => r.url !== target && !view.removed.has(r.id));
    const again = new Set(tree.filter((r) => r.url === target).map((r) => r.id));
    setView((v) => ({ ...v, removed: new Set([...[...v.removed].filter((id) => !again.has(id)), ...others.map((r) => r.id)]) }));
    if (others.length) {
      // Previous runs are cleared for good once Undo expires.
      const hidden = new Set(others.map((r) => r.id));
      const sites = new Set(others.map((r) => (r.url ? siteOf(r.url) : r.id))).size;
      const nodes = others.flatMap((r) => (r.type === "site" ? r.children : [r]));
      toastUndo(`Cleared ${plural(sites, "earlier site")}`, () => discard(nodes),
        () => setView((v) => ({ ...v, removed: new Set([...v.removed].filter((id) => !hidden.has(id))) })));
    }
    void data.explore(target, { headless: useBrowser });
  };
  // The engine finds the links (scheme-less ones included); one looks like a
  // page to explore, several or a direct file go in as links.
  const submit = async () => {
    const text = url.trim();
    if (!text) return;
    let urls: string[];
    try {
      urls = await extractLinks(text);
    } catch (error) {
      toast(`Couldn't read that: ${error instanceof Error ? error.message : String(error)}`, { tone: "danger" });
      return;
    }
    if (!urls.length) { toast("No link found in that text", { tone: "danger" }); return; }
    if (urls.length > 1 || looksLikeFile(urls[0])) {
      void props.onLinkAdd?.(urls.join("\n"));
      toast(`Added ${plural(urls.length, "link")} to Explore`);
    } else {
      exploreUrl(urls[0]);
    }
    setUrl("");
  };

  const onKeyDown = useExploreKeys({ rows, byId, view, selectedIds, focusedId, anchorId, setFocusedId, setAnchorId, setSelectedIds, selection,
    toggleOpen, openAll, primary, download: (n) => void download(n), remove, copy, showMenu, focusQuery: () => queryRef.current?.select() });

  // Ctrl+L / Ctrl+F reach the two inputs from anywhere on the page.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key.toLowerCase() === "l") { e.preventDefault(); urlRef.current?.select(); }
      if (e.key.toLowerCase() === "f") { e.preventDefault(); setShowFilters(true); requestAnimationFrame(() => queryRef.current?.select()); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const visibleFiles = rows.filter((r) => isDownloadable(r.node));
  const allState = visibleFiles.length && visibleFiles.every((r) => selectedIds.has(r.node.id)) ? "on" : visibleFiles.some((r) => selectedIds.has(r.node.id)) ? "mixed" : "off";
  const selectedSize = selection.reduce((sum, f) => sum + (f.size ?? 0), 0);

  return (
    <div className="xp-page">
      <form className="xp-head" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <label className="xp-url">
          <Icon d={ic.globe} size={14} />
          <input ref={urlRef} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="Paste a page, folder, shortlink or several links"
            aria-label="Page, folder or links to explore" spellCheck={false} />
          <kbd>Ctrl L</kbd>
          <button type="button" className="xp-recent-btn" disabled={!recent.length} title="Recently explored" aria-label="Recently explored links"
            onClick={(e) => {
              const r = (e.currentTarget.closest(".xp-url") as HTMLElement).getBoundingClientRect();
              setMenu({ x: r.left, y: r.bottom + 4, label: "Recently explored", items: [
                { heading: "Recently explored" },
                ...recent.map((u) => ({ label: u.replace(/^https?:\/\//, ""), icon: ic.history, onSelect: () => exploreUrl(u) })),
              ] });
            }}>
            <Icon d={ic.chevronDown} size={12} />
          </button>
        </label>
        <label className="xp-browser" title="Open the page in the background browser first (slower; for pages that need scripts or have countdowns)">
          <input type="checkbox" checked={useBrowser} onChange={(e) => setUseBrowser(e.target.checked)} /> Use browser
        </label>
        <button type="submit" className="btn-accent xp-go" disabled={!url.trim()}>Explore</button>
        <button type="button" className={`xp-icon-btn${showFilters ? " on" : ""}`} aria-pressed={showFilters} title="Filters"
          aria-label="Show filters" onClick={() => setShowFilters((v) => !v)}>
          <Icon d={ic.sliders} size={14} />
        </button>
      </form>

      <div className={`xp-body${showFilters ? "" : " no-filters"}`}>
        <TableScroll
          layout={columns}
          role="treegrid"
          tabIndex={0}
          aria-label="Explored links"
          aria-multiselectable="true"
          aria-activedescendant={focusedId ? `xr-${focusedId}` : undefined}
          className="xp-table"
          onKeyDown={onKeyDown}
          header={
            <ColumnHeader
              layout={columns}
              sortKey={view.sort?.key ?? null}
              sortDir={view.sort?.dir === 1 ? "asc" : "desc"}
              onSort={(key) => setView((v) => ({ ...v, sort: { key: key as SortKey, dir: v.sort?.key === key ? (v.sort.dir === 1 ? -1 : 1) : key === "name" || key === "host" || key === "kind" ? 1 : -1 } }))}
              leading={{ select: (
                <button type="button" className={`xp-check ${allState}`} role="checkbox" aria-checked={allState === "mixed" ? "mixed" : allState === "on"}
                  aria-label={`Select all ${visibleFiles.length} visible files`} title="Select all visible files (Ctrl+A)"
                  onClick={() => setSelectedIds((s) => setChecked(s, visibleFiles.map((r) => r.node), allState !== "on", view))} />
              ) }}
            />
          }
        >
          {rows.length === 0 && (
            <div className="xp-empty">
              {tree.length === 0 ? (
                <>
                  <Icon d={ic.globe} size={28} />
                  <p>Paste a page to find its downloads, or capture them from your browser.</p>
                  <p className="xp-sub">Buttons, mirrors and shortlinks show up as a tree you can follow step by step.</p>
                </>
              ) : (
                <p>Nothing matches these filters. <button type="button" className="xp-link" onClick={() => setView((v) => ({ ...v, kind: "all", hosts: [], query: "" }))}>Clear filters</button></p>
              )}
            </div>
          )}
          {rows.map((row) => {
            const n = row.node;
            return (
              <div key={`${n.id}@${row.parent?.id ?? ""}`}
                onMouseDown={(e) => {
                  if (e.button !== 0) return;
                  setFocusedId(n.id);
                  if (e.shiftKey && anchorId) {
                    const ids = rows.map((r) => r.node.id); const [a, b] = [ids.indexOf(anchorId), ids.indexOf(n.id)].sort((x, y) => x - y);
                    setSelectedIds((s) => setChecked(s, rows.slice(a, b + 1).map((r) => r.node), true, view));
                  } else if (e.ctrlKey || e.metaKey) setSelectedIds((s) => setChecked(s, [n], checkState(n, s, view) !== "on", view));
                  else setAnchorId(n.id);
                }}
                onDoubleClick={(e) => { if (!(e.target as HTMLElement).closest("[data-check],button")) primary(n); }}
                onContextMenu={(e) => { e.preventDefault(); showMenu(n, e.clientX, e.clientY); }}
              >
                <ExploreRow
                  row={row}
                  columns={columns}
                  check={checkState(n, selectedIds, view)}
                  open={isOpen(n, view)}
                  focused={focusedId === n.id}
                  fresh={fresh.isNew(n.id)}
                  summary={n.type === "site" || n.type === "source" ? summarize(n, view) : undefined}
                  queued={isDownloadable(n) && queued(n)}
                  onToggleOpen={() => toggleOpen(n)}
                  onCheck={(e) => {
                    const on = checkState(n, selectedIds, view) !== "on";
                    if (e.shiftKey && anchorId) {
                      const ids = rows.map((r) => r.node.id); const [a, b] = [ids.indexOf(anchorId), ids.indexOf(n.id)].sort((x, y) => x - y);
                      setSelectedIds((s) => setChecked(s, rows.slice(a, b + 1).map((r) => r.node), on, view));
                    } else setSelectedIds((s) => setChecked(s, [n], on, view));
                    setAnchorId(n.id); setFocusedId(n.id);
                  }}
                  onPrimary={() => (n.type === "source" ? actions.download(filesUnder(n, view).filter((f) => f.best).slice(0, 1)) : primary(n))}
                  onMenu={(e) => { const r = (e.currentTarget as HTMLElement).getBoundingClientRect(); showMenu(n, r.left, r.bottom + 2); }}
                />
              </div>
            );
          })}
        </TableScroll>
        {showFilters && <ExploreFilters view={view} counts={counts} onChange={(patch) => setView((v) => ({ ...v, ...patch }))} queryRef={queryRef} />}
      </div>

      <div className="xp-status" role="toolbar" aria-label="Selection">
        {selection.length ? (
          <>
            <span className="xp-status-text"><b>{selection.length}</b> selected{selectedSize ? ` · ${formatBytes(selectedSize)}` : ""}</span>
            <button type="button" className="xp-bar-btn" onClick={() => setSelectedIds(new Set())} title="Clear selection (Esc)">Clear</button>
            <span className="grow" />
            <button type="button" className="xp-bar-btn" onClick={() => copy(selection.map((s) => s.url).join("\n"), "addresses")}><Icon d={ic.copy} size={12} />Copy addresses</button>
            <button type="button" className="xp-bar-btn" onClick={() => void download(selection, { pickFolder: true })}><Icon d={ic.folder} size={12} />Download to…</button>
            <button type="button" className="btn-accent xp-bar-primary" onClick={() => { void download(selection); setSelectedIds(new Set()); }}>
              <Icon d={ic.download} size={12} />Add {plural(selection.length, "file")} to Downloads
            </button>
          </>
        ) : (
          <>
            <span className="xp-status-text">{plural(new Set(tree.map((t) => (t.url ? siteOf(t.url) : t.id))).size, "site")} · {plural(counts.kinds.all, "file")}{fresh.count > 0 && <span className="xp-new-count"> · {fresh.count} new</span>}</span>
            <span className="grow" />
            <span className="xp-legend" aria-hidden="true"><kbd>Ctrl A</kbd>select all <kbd>Ctrl D</kbd>deselect <kbd>Enter</kbd>download <kbd>Shift F10</kbd>menu</span>
          </>
        )}
      </div>

      {menu && <ActionMenu x={menu.x} y={menu.y} items={menu.items} label={menu.label} onClose={() => setMenu(null)} />}
      {props.onMediaPlan && props.onMediaImport && (
        <MediaCaptureOverlay
          candidate={mediaFor}
          open={Boolean(mediaFor)}
          onClose={() => setMediaFor(null)}
          onPlan={props.onMediaPlan}
          onImport={async (candidate, target) => {
            const result = await props.onMediaImport!(candidate, target);
            setMediaFor(null);
            toast("Added the stream to Downloads", { tone: "ok" });
            return result as Record<string, unknown>;
          }}
        />
      )}
    </div>
  );
}

