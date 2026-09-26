// Keyboard model for the Explore tree (WAI-ARIA treegrid conventions):
// ↑/↓ Home/End PgUp/PgDn move · →/← expand, collapse, go to parent/child ·
// Space checks (Shift extends) · Enter downloads or follows · Ctrl+A checks all
// · Esc / Ctrl+D clear · Del removes · Ctrl+C copies addresses · * expands below ·
// Menu/Shift+F10 opens the row menu · typing jumps to a matching name.
import React, { useCallback, useRef } from "react";
import { isMenuKey, menuAnchor } from "../../ui/ActionMenu";
import type { ExploreNode } from "./exploreModel";
import { checkState, isDownloadable, isOpen, setChecked, type Row, type ViewState } from "./exploreView";

const PAGE = 12;

export function useExploreKeys(p: {
  rows: Row[];
  byId: Map<string, ExploreNode>;
  view: ViewState;
  selectedIds: Set<string>;
  focusedId: string | null;
  anchorId: string | null;
  setFocusedId: (id: string) => void;
  setAnchorId: (id: string) => void;
  setSelectedIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  selection: ExploreNode[];
  toggleOpen: (n: ExploreNode, open?: boolean) => void;
  openAll: (n: ExploreNode, open: boolean) => void;
  primary: (n: ExploreNode) => void;
  download: (nodes: ExploreNode[]) => void;
  remove: (nodes: ExploreNode[]) => void;
  copy: (text: string, what: string) => void;
  showMenu: (n: ExploreNode, x: number, y: number) => void;
  focusQuery: () => void;
}) {
  const typed = useRef({ text: "", at: 0 });

  return useCallback((e: React.KeyboardEvent) => {
    const { rows, view } = p;
    if (!rows.length) return;
    const index = rows.findIndex((r) => r.node.id === p.focusedId);
    const row = rows[index];
    const n = row?.node;
    const scrollTo = (id: string) => requestAnimationFrame(() => document.getElementById(`xr-${id}`)?.scrollIntoView({ block: "nearest" }));
    const go = (to: number) => {
      e.preventDefault();
      const next = rows[Math.max(0, Math.min(rows.length - 1, to))].node;
      if (e.shiftKey && p.anchorId) {
        const ids = rows.map((r) => r.node.id);
        const [a, b] = [ids.indexOf(p.anchorId), ids.indexOf(next.id)].sort((x, y) => x - y);
        p.setSelectedIds((s) => setChecked(s, rows.slice(a, b + 1).map((r) => r.node), true, view));
      } else if (!e.shiftKey) p.setAnchorId(next.id);
      p.setFocusedId(next.id);
      scrollTo(next.id);
    };
    const ctrl = e.ctrlKey || e.metaKey;

    if (e.key === "ArrowDown") return go(index + 1);
    if (e.key === "ArrowUp") return go(index < 0 ? 0 : index - 1);
    if (e.key === "Home") return go(0);
    if (e.key === "End") return go(rows.length - 1);
    if (e.key === "PageDown") return go(index + PAGE);
    if (e.key === "PageUp") return go(index - PAGE);
    if (!n) { if (e.key === "Enter" || e.key === " ") go(0); return; }

    if (isMenuKey(e)) {
      e.preventDefault();
      const el = document.getElementById(`xr-${n.id}`);
      if (el) { const at = menuAnchor(el); p.showMenu(n, at.x, at.y); }
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      if (n.children.length && !isOpen(n, view)) p.toggleOpen(n, true);
      else if (rows[index + 1]?.parent?.id === n.id) go(index + 1);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (n.children.length && isOpen(n, view)) p.toggleOpen(n, false);
      else if (row.parent) { const at = rows.findIndex((r) => r.node.id === row.parent!.id); if (at >= 0) go(at); }
    } else if (e.key === " ") {
      e.preventDefault();
      const on = checkState(n, p.selectedIds, view) !== "on";
      p.setSelectedIds((s) => setChecked(s, [n], on, view));
      p.setAnchorId(n.id);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (p.selection.length > 1 && p.selection.some((s) => s.id === n.id)) p.download(p.selection);
      else p.primary(n);
    } else if (e.key === "Escape") {
      if (p.selectedIds.size) { e.preventDefault(); p.setSelectedIds(new Set()); }
    } else if (e.key === "Delete") {
      e.preventDefault();
      p.remove(p.selection.some((s) => s.id === n.id) ? p.selection : [n]);
    } else if (e.key === "*") {
      p.openAll(n, true);
    } else if (ctrl && e.key.toLowerCase() === "a") {
      e.preventDefault();
      p.setSelectedIds((s) => setChecked(s, rows.map((r) => r.node).filter(isDownloadable), true, view));
    } else if (ctrl && e.key.toLowerCase() === "d") {
      e.preventDefault();
      p.setSelectedIds(new Set());
    } else if (ctrl && e.key.toLowerCase() === "c") {
      e.preventDefault();
      const targets = p.selection.length ? p.selection : n.url ? [n] : [];
      if (targets.length) p.copy(targets.map((t) => t.url).join("\n"), targets.length > 1 ? "addresses" : "address");
    } else if (e.key === "/" ) {
      e.preventDefault();
      p.focusQuery();
    } else if (e.key.length === 1 && !ctrl && !e.altKey) {
      const now = Date.now();
      typed.current = { text: now - typed.current.at < 700 ? typed.current.text + e.key.toLowerCase() : e.key.toLowerCase(), at: now };
      const q = typed.current.text;
      const order = [...rows.slice(index + (q.length === 1 ? 1 : 0)), ...rows.slice(0, index + 1)];
      const hit = order.find((r) => r.node.label.replace(/^[“"]/, "").toLowerCase().startsWith(q));
      if (hit) { p.setFocusedId(hit.node.id); p.setAnchorId(hit.node.id); scrollTo(hit.node.id); }
    }
  }, [p]);
}
