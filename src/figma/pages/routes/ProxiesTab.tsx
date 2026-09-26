// Proxies: every proxy route in one table on the shared column system, with
// checkbox selection, bulk actions and engine-run checks.
import React, { useMemo, useState } from "react";
import type { RouteProfile } from "../../../api";
import { Icon, ic } from "../../icons";
import { ColumnCells, ColumnHeader, TableScroll, useColumnLayout } from "../../ui/ColumnHeader";
import { PROXY_COLUMNS, PROXY_COLUMNS_KEY } from "../../ui/downloadColumns";
import { ActionMenu, isMenuKey, menuAnchor, type MenuEntry } from "../../ui/ActionMenu";
import { toast } from "../../ui/Toasts";
import { copyText } from "../../ui/copyText";
import { ProxyImportSheet } from "./ProxyImportSheet";
import { healthDot, latencyClass } from "./ProviderLocations";
import type { useConnections } from "./useConnections";

type Data = ReturnType<typeof useConnections>;
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
const address = (r: RouteProfile) => r.endpoint?.replace(/^\w+:\/\//, "") ?? r.id;
const ago = (ms?: number) => { if (!ms) return "—"; const s = Math.round((Date.now() - ms) / 1000); return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; };

export function ProxiesTab({ data, proxies, activeId, onUse }: { data: Data; proxies: RouteProfile[]; activeId: string; onUse: (r: RouteProfile) => void }) {
  const columns = useColumnLayout(PROXY_COLUMNS_KEY, PROXY_COLUMNS);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [anchorId, setAnchorId] = useState<string | null>(null);
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 }>({ key: "address", dir: 1 });
  const [sheet, setSheet] = useState(false);
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuEntry[]; label: string } | null>(null);

  const rows = useMemo(() => {
    const key = (r: RouteProfile) => sort.key === "latency" ? data.health[r.id]?.latencyMs ?? 1e9 : sort.key === "type" ? r.kind : address(r);
    return [...proxies].sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * sort.dir);
  }, [proxies, sort, data.health]);
  const selected = rows.filter((r) => selectedIds.has(r.id));
  const counts = { alive: 0, dead: 0, unchecked: 0 };
  for (const r of proxies) { const h = data.health[r.id]; if (!h) counts.unchecked++; else if (h.healthy) counts.alive++; else counts.dead++; }

  const toggle = (ids: string[], on: boolean) => setSelectedIds((s) => { const n = new Set(s); ids.forEach((id) => (on ? n.add(id) : n.delete(id))); return n; });
  const clickSelect = (r: RouteProfile, e: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean }, viaCheckbox: boolean) => {
    setFocusedId(r.id);
    if (e.shiftKey && anchorId) {
      const ids = rows.map((x) => x.id); const [a, b] = [ids.indexOf(anchorId), ids.indexOf(r.id)].sort((x, y) => x - y);
      toggle(ids.slice(a, b + 1), true);
    } else if (viaCheckbox || e.ctrlKey || e.metaKey) { toggle([r.id], !selectedIds.has(r.id)); setAnchorId(r.id); }
    else setAnchorId(r.id);
  };

  const removeRoutes = (list: RouteProfile[]) => {
    toggle(list.map((r) => r.id), false);
    data.remove(list, list.length === 1 ? address(list[0]) : plural(list.length, "proxy"));
  };
  const targets = (r: RouteProfile) => (selectedIds.has(r.id) && selected.length > 1 ? selected : [r]);
  const rowMenu = (r: RouteProfile): MenuEntry[] => {
    const t = targets(r); const many = t.length > 1;
    return [
      ...(many ? [{ heading: plural(t.length, "proxy") }] : []),
      { label: many ? `Check ${t.length} now` : "Check now", icon: ic.refreshCw, kbd: "Enter", onSelect: () => void data.checkMany(t) },
      ...(!many ? [{ label: "Use for downloads", icon: ic.power, checked: r.id === activeId, onSelect: () => onUse(r) }] : []),
      many ? { label: "Enable all", onSelect: () => t.forEach((x) => void data.setEnabled(x, true)) }
           : { label: "Enabled", kbd: "Space", checked: r.enabled !== false, onSelect: () => void data.setEnabled(r, r.enabled === false) },
      ...(many ? [{ label: "Disable all", onSelect: () => t.forEach((x) => void data.setEnabled(x, false)) }] : []),
      "-",
      { label: many ? `Copy ${t.length} addresses` : "Copy address", icon: ic.copy, kbd: "Ctrl+C", onSelect: () => copyText(t.map(address).join("\n"), many ? "addresses" : "address") },
      { label: "Copy as URL", onSelect: () => copyText(t.map((x) => x.endpoint ?? "").join("\n"), "URL") },
      "-",
      { label: many ? `Remove ${t.length} proxies` : "Remove", icon: ic.trash, kbd: "Del", danger: true, onSelect: () => void removeRoutes(t) },
    ];
  };

  const onKey = (e: React.KeyboardEvent) => {
    const i = rows.findIndex((r) => r.id === focusedId); const r = rows[i];
    const go = (to: number) => {
      e.preventDefault(); const next = rows[Math.max(0, Math.min(rows.length - 1, to))]; if (!next) return;
      if (e.shiftKey && anchorId) clickSelect(next, { shiftKey: true, ctrlKey: false, metaKey: false }, false); else { setFocusedId(next.id); setAnchorId(next.id); }
      document.getElementById(`px-${next.id}`)?.scrollIntoView({ block: "nearest" });
    };
    if (e.key === "ArrowDown") go(i + 1);
    else if (e.key === "ArrowUp") go(i < 0 ? 0 : i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(rows.length - 1);
    else if (!r) return;
    else if (e.key === " ") { e.preventDefault(); toggle([r.id], !selectedIds.has(r.id)); setAnchorId(r.id); }
    else if (e.key === "Enter") { e.preventDefault(); void data.checkMany(targets(r)); }
    else if (e.key === "Delete") { e.preventDefault(); void removeRoutes(targets(r)); }
    else if (e.key === "Escape") setSelectedIds(new Set());
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") { e.preventDefault(); toggle(rows.map((x) => x.id), true); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "c") { e.preventDefault(); copyText(targets(r).map(address).join("\n"), "addresses"); }
    else if (isMenuKey(e)) { e.preventDefault(); const el = document.getElementById(`px-${r.id}`); if (el) setMenu({ ...menuAnchor(el), items: rowMenu(r), label: `Actions for ${address(r)}` }); }
  };

  const allState = rows.length && selected.length === rows.length ? "on" : selected.length ? "mixed" : "off";

  return (
    <div className="cx-proxies">
      <div className="cx-toolbar" role="toolbar" aria-label="Proxy actions">
        <button type="button" className="btn-accent ui-primary" onClick={() => setSheet(true)}><Icon d={ic.plus} size={12} />Add proxies</button>
        <button type="button" className="ui-btn" disabled={!proxies.length} onClick={() => void data.checkMany(proxies)}><Icon d={ic.refreshCw} size={12} />Check all</button>
        <span className="grow" />
        <span className="cx-summary" aria-live="polite">
          {counts.alive > 0 && <span><span className="ui-dot ok" aria-hidden="true" />{counts.alive} working</span>}
          {counts.dead > 0 && <span><span className="ui-dot bad" aria-hidden="true" />{counts.dead} failing</span>}
          {counts.unchecked > 0 && <span><span className="ui-dot muted" aria-hidden="true" />{counts.unchecked} not checked</span>}
        </span>
      </div>
      {selected.length > 0 && (
        <div className="cx-bulk" role="toolbar" aria-label="Selected proxies">
          <span><b>{selected.length}</b> selected</span>
          <button type="button" className="ui-btn ghost" onClick={() => void data.checkMany(selected)}><Icon d={ic.refreshCw} size={12} />Check</button>
          <button type="button" className="ui-btn ghost" onClick={() => selected.forEach((r) => void data.setEnabled(r, true))}>Enable</button>
          <button type="button" className="ui-btn ghost" onClick={() => selected.forEach((r) => void data.setEnabled(r, false))}>Disable</button>
          <button type="button" className="ui-btn ghost" onClick={() => copyText(selected.map(address).join("\n"), "addresses")}><Icon d={ic.copy} size={12} />Copy</button>
          <button type="button" className="ui-btn ghost danger" onClick={() => void removeRoutes(selected)}><Icon d={ic.trash} size={12} />Remove</button>
          <button type="button" className="ui-btn ghost" onClick={() => setSelectedIds(new Set())} title="Clear selection (Esc)">Clear</button>
        </div>
      )}
      <div className="cx-proxies-body">
        <TableScroll
          layout={columns}
          role="grid"
          aria-label="Proxies"
          aria-multiselectable="true"
          tabIndex={0}
          aria-activedescendant={focusedId ? `px-${focusedId}` : undefined}
          className="cx-table"
          onKeyDown={onKey}
          header={<ColumnHeader layout={columns} sortKey={sort.key} sortDir={sort.dir === 1 ? "asc" : "desc"}
            onSort={(key) => setSort((s) => ({ key, dir: s.key === key ? (s.dir === 1 ? -1 : 1) : 1 }))}
            leading={{ select: <button type="button" className={`xp-check ${allState}`} role="checkbox" aria-checked={allState === "mixed" ? "mixed" : allState === "on"}
              aria-label="Select all proxies" onClick={() => (allState === "on" ? setSelectedIds(new Set()) : toggle(rows.map((r) => r.id), true))} /> }} />}
        >
          {!rows.length && (
            <div className="ui-empty-table">
              <p>No proxies yet.</p>
              <button type="button" className="ui-link" onClick={() => setSheet(true)}>Paste a list</button>
            </div>
          )}
          {rows.map((r) => {
            const h = data.health[r.id];
            const on = selectedIds.has(r.id);
            return (
              <div
                key={r.id}
                id={`px-${r.id}`}
                role="row"
                aria-selected={on}
                className={`dt-row cx-row${on ? " selected" : ""}${focusedId === r.id ? " focused" : ""}${r.enabled === false ? " off" : ""}${r.id === activeId ? " current" : ""}`}
                onMouseDown={(e) => { if (e.button === 0 && !(e.target as HTMLElement).closest("button")) clickSelect(r, e, false); }}
                onDoubleClick={() => void data.check(r)}
                onContextMenu={(e) => { e.preventDefault(); setFocusedId(r.id); setMenu({ x: e.clientX, y: e.clientY, items: rowMenu(r), label: `Actions for ${address(r)}` }); }}
              >
                <ColumnCells
                  layout={columns}
                  cells={{
                    select: <span className={`xp-check ${on ? "on" : "off"}`} aria-hidden="true" onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); clickSelect(r, e, true); }} />,
                    status: <span className={`ui-dot ${healthDot(h)}`} title={h?.error ?? (h ? (h.healthy ? "Working" : "Failing") : "Not checked")} />,
                    address: (
                      <span className="cx-cell-name">
                        <span className="mono truncate">{address(r)}</span>
                        {r.credential_ref && <span title="Uses a login" aria-label="Uses a login"><Icon d={ic.key} size={11} /></span>}
                        {r.id === activeId && <span className="ui-chip ok">in use</span>}
                        {h && !h.healthy && h.error && <span className="cx-err truncate" title={h.error}>{h.error}</span>}
                      </span>
                    ),
                    type: <span className="cx-type">{r.kind === "http_proxy" ? "HTTP" : "SOCKS5"}</span>,
                    latency: <span className={`mono ${latencyClass(h?.latencyMs)}`}>{h?.checking ? "…" : h?.latencyMs != null && h.healthy ? `${h.latencyMs} ms` : "—"}</span>,
                    ip: <span className="mono cx-muted">{h?.ip ?? "—"}</span>,
                    checked: <span className="cx-muted">{h?.checking ? "checking…" : ago(h?.checkedAt)}</span>,
                    enabled: (
                      <button type="button" role="switch" aria-checked={r.enabled !== false} aria-label={`Use ${address(r)}`} tabIndex={-1}
                        className={`mini-switch${r.enabled !== false ? " on" : ""}`} onClick={() => void data.setEnabled(r, r.enabled === false)} />
                    ),
                    actions: (
                      <button type="button" className="cx-icon" tabIndex={-1} aria-label="More actions" title="More actions (Shift+F10)"
                        onClick={(e) => { const b = (e.currentTarget as HTMLElement).getBoundingClientRect(); setMenu({ x: b.left, y: b.bottom + 2, items: rowMenu(r), label: `Actions for ${address(r)}` }); }}>
                        <Icon d={ic.dots} size={12} />
                      </button>
                    ),
                  }}
                />
              </div>
            );
          })}
        </TableScroll>
        {sheet && (
          <ProxyImportSheet
            onClose={() => setSheet(false)}
            onImport={async (list) => {
              const saved = await data.importProxies(list);
              setSheet(false);
              toast(`Imported ${plural(saved.length, "proxy")} — checking them now`, { tone: "ok" });
              void data.checkMany(saved);
            }}
          />
        )}
      </div>
      {menu && <ActionMenu x={menu.x} y={menu.y} items={menu.items} label={menu.label} onClose={() => setMenu(null)} />}
    </div>
  );
}
