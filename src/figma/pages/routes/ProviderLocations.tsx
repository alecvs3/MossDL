// A configured provider: its locations in a table on the shared column
// system (radio picks a server), with the connection panel on the right.
import React, { useEffect, useMemo, useState } from "react";
import type { RouteProfile } from "../../../api";
import { Icon, ic } from "../../icons";
import { ColumnCells, ColumnHeader, TableScroll, useColumnLayout } from "../../ui/ColumnHeader";
import { LOCATION_COLUMNS, LOCATION_COLUMNS_KEY } from "../../ui/downloadColumns";
import { isMenuKey, menuAnchor, type MenuEntry } from "../../ui/ActionMenu";
import { toast } from "../../ui/Toasts";
import { copyText } from "../../ui/copyText";
import { MODE_TEXT, type Provider } from "./providers";
import { ProviderLogo } from "./ProviderLogo";
import { ConnectionPanel, healthDot, latencyClass } from "./ConnectionPanel";
import type { RouteSettings } from "./ConnectionsSettings";
import type { useConnections } from "./useConnections";

export { healthDot, latencyClass } from "./ConnectionPanel";

type Data = ReturnType<typeof useConnections>;

export function ProviderLocations({ entry, provider, data, activeId, settings, onUse, onSettingChange, onImportMore, onMenu }: {
  entry: { id: string; name: string; kind?: "direct" | "custom"; routes: RouteProfile[] };
  provider?: Provider;
  data: Data;
  activeId: string;
  settings: RouteSettings;
  onUse: (r: RouteProfile) => void;
  onSettingChange: (key: keyof RouteSettings, value: string | boolean) => void;
  onImportMore: () => void;
  onMenu: (x: number, y: number, items: MenuEntry[], label: string) => void;
}) {
  const columns = useColumnLayout(LOCATION_COLUMNS_KEY, LOCATION_COLUMNS);
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 }>({ key: "name", dir: 1 });
  const isActive = (r: RouteProfile) => r.id === activeId || (r.kind === "direct" && (activeId === "auto" || activeId === "direct"));
  const active = data.routes.find(isActive);
  const [pickedId, setPickedId] = useState<string | null>(null);
  const picked = entry.routes.find((r) => r.id === pickedId) ?? entry.routes.find(isActive) ?? entry.routes[0];
  const direct = data.routes.find((r) => r.kind === "direct");
  useEffect(() => setPickedId(null), [entry.id]);

  const rows = useMemo(() => {
    const key = (r: RouteProfile) => sort.key === "latency" ? data.health[r.id]?.latencyMs ?? 1e9 : sort.key === "endpoint" ? r.endpoint ?? "" : (r.region || r.id).toLowerCase();
    return [...entry.routes].sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * sort.dir);
  }, [entry.routes, sort, data.health]);

  const nextBest = async () => {
    const next = await data.nextBest(activeId);
    if (next) { onUse(next); setPickedId(next.id); toast(`Downloads now use ${next.region || next.id}`, { tone: "ok" }); }
    else toast("No other healthy connection right now");
  };

  const rowMenu = (r: RouteProfile): MenuEntry[] => [
    { label: "Connect", icon: ic.power, kbd: "Enter", checked: isActive(r), onSelect: () => onUse(r) },
    { label: "Check now", icon: ic.refreshCw, onSelect: () => void data.check(r) },
    "-",
    { label: r.enabled === false ? "Include in switching" : "Skip when switching", checked: r.enabled !== false, onSelect: () => void data.setEnabled(r, r.enabled === false) },
    { label: "Copy server address", icon: ic.copy, disabled: !r.endpoint, onSelect: () => copyText(r.endpoint ?? "", "server address") },
    ...(data.health[r.id]?.ip ? [{ label: "Copy exit IP", icon: ic.copy, onSelect: () => copyText(data.health[r.id]!.ip!, "exit IP") }] : []),
    ...(r.kind !== "direct" ? ["-" as const, { label: "Remove location", icon: ic.trash, kbd: "Del", danger: true, onSelect: () => data.remove([r], r.region || r.id) }] : []),
  ];

  const onKey = (e: React.KeyboardEvent) => {
    const i = rows.findIndex((r) => r.id === picked?.id);
    const go = (to: number) => { e.preventDefault(); const r = rows[Math.max(0, Math.min(rows.length - 1, to))]; setPickedId(r.id); document.getElementById(`loc-${r.id}`)?.scrollIntoView({ block: "nearest" }); };
    const r = rows[i];
    if (e.key === "ArrowDown") go(i + 1);
    else if (e.key === "ArrowUp") go(i < 0 ? 0 : i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(rows.length - 1);
    else if (r && e.key === "Enter") { e.preventDefault(); onUse(r); }
    else if (r && e.key === "Delete" && r.kind !== "direct") { e.preventDefault(); (rowMenu(r).at(-1) as { onSelect: () => void }).onSelect(); }
    else if (r && isMenuKey(e)) { e.preventDefault(); const el = document.getElementById(`loc-${r.id}`); if (el) { const at = menuAnchor(el); onMenu(at.x, at.y, rowMenu(r), `Actions for ${r.region || r.id}`); } }
  };

  return (
    <div className="cx-locations-3">
      <div className="cx-locations">
        <div className="cx-detail-top">
          <ProviderLogo provider={provider} kind={entry.kind} size={40} />
          <div className="cx-detail-title">
            <h2>{entry.name}</h2>
            <div className="ui-chips">
              {provider && <span className={`ui-chip${provider.mode === "any" ? " ok" : ""}`}>{MODE_TEXT[provider.mode]}</span>}
            </div>
          </div>
          <span className="cx-pill"><Icon d={ic.mapPin} size={11} /> {entry.routes.length} location{entry.routes.length === 1 ? "" : "s"}</span>
          {entry.kind !== "direct" && <button type="button" className="ui-btn" onClick={onImportMore}><Icon d={ic.upload} size={12} />Import more</button>}
        </div>

        <TableScroll
          layout={columns}
          role="grid"
          aria-label={`${entry.name} locations`}
          tabIndex={0}
          aria-activedescendant={picked ? `loc-${picked.id}` : undefined}
          className="cx-table"
          onKeyDown={onKey}
          header={<ColumnHeader layout={columns} sortKey={sort.key} sortDir={sort.dir === 1 ? "asc" : "desc"}
            onSort={(key) => setSort((s) => ({ key, dir: s.key === key ? (s.dir === 1 ? -1 : 1) : 1 }))} />}
        >
          {rows.map((r) => {
            const h = data.health[r.id];
            const on = picked?.id === r.id;
            const inUse = isActive(r);
            return (
              <div
                key={r.id}
                id={`loc-${r.id}`}
                role="row"
                aria-selected={on}
                className={`dt-row cx-row${on ? " picked" : ""}${inUse ? " current" : ""}${r.enabled === false ? " off" : ""}`}
                onMouseDown={() => setPickedId(r.id)}
                onDoubleClick={() => onUse(r)}
                onContextMenu={(e) => { e.preventDefault(); setPickedId(r.id); onMenu(e.clientX, e.clientY, rowMenu(r), `Actions for ${r.region || r.id}`); }}
              >
                <ColumnCells
                  layout={columns}
                  cells={{
                    status: <span className={`cx-radio${on ? " on" : ""}`} aria-hidden="true" />,
                    name: (
                      <span className="cx-cell-name">
                        <span className={`ui-dot ${healthDot(h)}`} title={h?.error ?? (h ? (h.healthy ? "Up" : "Down") : "Not checked")} />
                        <span className="truncate">{r.region || (r.kind === "direct" ? "Direct" : r.id)}</span>
                        {inUse && <span className="ui-chip ok">in use</span>}
                        {r.enabled === false && <span className="ui-chip">skipped when switching</span>}
                      </span>
                    ),
                    endpoint: <span className="mono truncate cx-muted">{r.endpoint ?? "—"}</span>,
                    ip: <span className="mono cx-muted">{h?.ip ?? "—"}</span>,
                    latency: <span className={`mono ${latencyClass(h?.latencyMs)}`}>{h?.checking ? "…" : h?.latencyMs != null ? `${h.latencyMs} ms` : "—"}</span>,
                    checked: <span className="cx-muted">{h?.checking ? "checking…" : h?.checkedAt ? new Date(h.checkedAt).toLocaleTimeString() : "—"}</span>,
                    actions: (
                      <button type="button" className="cx-icon" tabIndex={-1} aria-label="More actions" title="More actions (Shift+F10)"
                        onClick={(e) => { const b = (e.currentTarget as HTMLElement).getBoundingClientRect(); onMenu(b.left, b.bottom + 2, rowMenu(r), `Actions for ${r.region || r.id}`); }}>
                        <Icon d={ic.dots} size={12} />
                      </button>
                    ),
                  }}
                />
              </div>
            );
          })}
        </TableScroll>
      </div>

      <ConnectionPanel
        picked={picked}
        pickedHealth={picked ? data.health[picked.id] : undefined}
        active={active}
        activeHealth={active ? data.health[active.id] : undefined}
        inUse={Boolean(picked && isActive(picked))}
        canDisconnect={Boolean(active && active.kind !== "direct" && direct)}
        settings={settings}
        onConnect={() => picked && onUse(picked)}
        onDisconnect={() => direct && onUse(direct)}
        onNextBest={() => void nextBest()}
        onCheckAll={() => void data.checkMany(entry.routes)}
        onCheckPicked={() => picked && void data.check(picked)}
        onSettingChange={onSettingChange}
      />
    </div>
  );
}
