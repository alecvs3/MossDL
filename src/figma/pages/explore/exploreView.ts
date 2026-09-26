// Turns the Explore tree into visible rows: filtering, sorting, alternative
// groupings, expansion and tri-state checkbox state. Pure functions only.
import { KINDS } from "../../../lib/fileKinds.ts";
import { siteOf, walk, type ExploreNode } from "./exploreModel.ts";

export type GroupBy = "site" | "source" | "kind" | "host";
export type SizeBucket = "any" | "small" | "medium" | "large";
export type SortKey = "name" | "kind" | "size" | "host" | "match" | "added";

export interface ViewState {
  kind: string;
  /** Hosts to show; empty means every host. */
  hosts: string[];
  query: string;
  size: SizeBucket;
  groupBy: GroupBy;
  showLow: boolean;
  sort: { key: SortKey; dir: 1 | -1 } | null;
  open: Record<string, boolean>;
  removed: Set<string>;
}

export interface Row { node: ExploreNode; depth: number; parent: ExploreNode | null; flat: boolean }

export const initialView: ViewState = {
  kind: "all", hosts: [], query: "", size: "any", groupBy: "site", showLow: true, sort: null, open: {}, removed: new Set(),
};

/** Rows the user can send to Downloads. */
export const isDownloadable = (n: ExploreNode) => n.type === "file" && Boolean(n.url);

/**
 * What starts open: anything the engine is working on, pages the user asked
 * for, and whatever leads to a high-scoring download. Everything else starts
 * collapsed so the promising rows are what you see first.
 */
export function isOpen(n: ExploreNode, view: ViewState): boolean {
  if (n.id in view.open) return view.open[n.id];
  if (n.busy || n.children.some((c) => c.busy)) return true;
  if (n.low) return false;
  if (n.openByDefault) return true;
  if (n.type === "source" || n.type === "site") {
    return Boolean(n.hot) || n.origin.kind === "crawl" || n.via === "Pasted" || n.children.some((c) => c.origin.kind === "crawl");
  }
  return Boolean(n.hot);
}

const hidden = (n: ExploreNode, view: ViewState) => view.removed.has(n.id) || (Boolean(n.low) && !view.showLow);
const filtering = (view: ViewState) => view.kind !== "all" || view.hosts.length > 0 || Boolean(view.query) || view.size !== "any";
const MB = 1024 * 1024;
const SIZE_TEST: Record<SizeBucket, (bytes: number) => boolean> = {
  any: () => true, small: (b) => b < MB, medium: (b) => b >= MB && b <= 100 * MB, large: (b) => b > 100 * MB,
};

function matches(n: ExploreNode, view: ViewState): boolean {
  if (!isDownloadable(n)) return false;
  if (view.kind !== "all" && n.kind !== view.kind) return false;
  if (view.hosts.length && !view.hosts.includes(n.host ?? "")) return false;
  if (view.size !== "any" && (n.size == null || !SIZE_TEST[view.size](n.size))) return false;
  if (view.query && !`${n.label} ${n.url ?? ""}`.toLowerCase().includes(view.query.toLowerCase())) return false;
  return true;
}
function visible(n: ExploreNode, view: ViewState): boolean {
  if (hidden(n, view)) return false;
  if (!filtering(view)) return true;
  return matches(n, view) || n.children.some((c) => visible(c, view));
}

const SORT: Record<SortKey, (n: ExploreNode) => string | number> = {
  name: (n) => n.label.toLowerCase(),
  kind: (n) => n.ext ?? "",
  size: (n) => n.size ?? -1,
  host: (n) => n.host ?? "",
  match: (n) => n.match ?? -1,
  added: (n) => n.addedAt ?? 0,
};
function sorted(list: ExploreNode[], view: ViewState): ExploreNode[] {
  if (!view.sort) return list;
  const key = SORT[view.sort.key], dir = view.sort.dir;
  return [...list].sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * dir);
}

/** "Group by kind/host" re-slices the same file rows into virtual groups. */
function regroup(tree: ExploreNode[], view: ViewState): ExploreNode[] {
  if (view.groupBy === "source") return tree;
  if (view.groupBy === "site") return bySite(tree);
  const groups = new Map<string, ExploreNode[]>();
  walk(tree, (n) => {
    if (!isDownloadable(n)) return;
    const key = (view.groupBy === "kind" ? n.kind : n.host) || "other";
    groups.set(key, [...(groups.get(key) ?? []), n]);
  });
  return [...groups].map(([key, children]) => ({
    id: `by-${view.groupBy}:${key}`, type: "group" as const, children, origin: { kind: "none" as const },
    label: view.groupBy === "kind" ? KINDS[key]?.label ?? key : key,
  }));
}

/**
 * One header per site; a site with a single page shows that page directly,
 * a site with several pages lists them underneath.
 */
function bySite(tree: ExploreNode[]): ExploreNode[] {
  const sites = new Map<string, ExploreNode[]>();
  const order: string[] = [];
  for (const root of tree) {
    const key = root.url ? siteOf(root.url) : root.id;
    if (!sites.has(key)) { sites.set(key, []); order.push(key); }
    sites.get(key)!.push(root);
  }
  return order.map((key) => {
    const pages = sites.get(key)!;
    if (pages.length === 1) return pages[0];
    return {
      id: `site:${key}`, type: "site" as const, label: key, host: key, url: `https://${key}`, children: pages,
      origin: { kind: "none" as const }, hot: pages.some((p) => p.hot),
      addedAt: Math.max(...pages.map((p) => p.addedAt ?? 0)), batchIds: pages.flatMap((p) => p.batchIds ?? []),
    };
  });
}

export function visibleRows(tree: ExploreNode[], view: ViewState): Row[] {
  const rows: Row[] = [];
  const virtual = view.groupBy === "kind" || view.groupBy === "host";
  const visit = (n: ExploreNode, depth: number, parent: ExploreNode | null, flat: boolean) => {
    if (!visible(n, view)) return;
    rows.push({ node: n, depth, parent, flat });
    if (!(isOpen(n, view) || filtering(view)) || !n.children.length) return;
    for (const child of sorted(n.children, view)) {
      if (virtual) { if (visible(child, view)) rows.push({ node: child, depth: depth + 1, parent: n, flat: true }); }
      else visit(child, depth + 1, n, false);
    }
  };
  for (const root of regroup(tree, view)) visit(root, 0, null, false);
  return rows;
}

/** Downloadable files at or under a node, respecting what is hidden. */
export function filesUnder(n: ExploreNode, view: ViewState): ExploreNode[] {
  const out: ExploreNode[] = [];
  walk([n], (x) => { if (isDownloadable(x) && !hidden(x, view)) out.push(x); });
  return out;
}

export type CheckState = "on" | "off" | "mixed";
export function checkState(n: ExploreNode, selected: Set<string>, view: ViewState): CheckState {
  const files = filesUnder(n, view);
  if (!files.length) return selected.has(n.id) ? "on" : "off";
  const count = files.filter((f) => selected.has(f.id)).length;
  return count === 0 ? "off" : count === files.length ? "on" : "mixed";
}

/** Checking a parent checks everything beneath it; unchecking clears it all. */
export function setChecked(selected: Set<string>, nodes: ExploreNode[], on: boolean, view: ViewState): Set<string> {
  const next = new Set(selected);
  for (const n of nodes) {
    walk([n], (x) => { if (!hidden(x, view)) { if (on) next.add(x.id); else next.delete(x.id); } });
  }
  return next;
}

/** Every downloadable file the selection covers, de-duplicated. */
export function selectedFiles(tree: ExploreNode[], selected: Set<string>, view: ViewState): ExploreNode[] {
  const out = new Map<string, ExploreNode>();
  walk(tree, (n) => { if (selected.has(n.id) && isDownloadable(n) && !hidden(n, view)) out.set(n.id, n); });
  return [...out.values()];
}

export function facetCounts(tree: ExploreNode[], view: ViewState) {
  const kinds: Record<string, number> = { all: 0 };
  const hosts: Record<string, number> = {};
  walk(tree, (n) => {
    // Noise (bot challenges, analytics) is not something to filter by.
    if (!isDownloadable(n) || n.low || view.removed.has(n.id)) return;
    kinds.all++;
    const k = n.kind || "other";
    kinds[k] = (kinds[k] ?? 0) + 1;
    if (n.host) hosts[n.host] = (hosts[n.host] ?? 0) + 1;
  });
  return { kinds, hosts };
}

/** What a header row summarises: file count, total known size, best score. */
export function summarize(n: ExploreNode, view: ViewState) {
  const files = filesUnder(n, view);
  return {
    files: files.length,
    size: files.reduce((sum, f) => sum + (f.size ?? 0), 0),
    best: files.reduce((m, f) => Math.max(m, f.match ?? 0), 0),
    pages: n.type === "site" ? n.children.length : 0,
  };
}
