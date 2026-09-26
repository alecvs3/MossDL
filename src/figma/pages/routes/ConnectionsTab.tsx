// Left: your connections and every supported provider. Right: the selected
// provider's locations, or its setup guide when nothing is imported yet.
import React, { useMemo, useRef, useState } from "react";
import type { RouteProfile } from "../../../api";
import { Icon, ic } from "../../icons";
import { ActionMenu, isMenuKey, menuAnchor, type MenuEntry } from "../../ui/ActionMenu";
import { toast } from "../../ui/Toasts";
import { PROVIDERS, providerById, providerOfRoute, type Provider } from "./providers";
import { ProviderLogo } from "./ProviderLogo";
import { ProviderSetup } from "./ProviderSetup";
import { ProviderLocations } from "./ProviderLocations";
import type { useConnections } from "./useConnections";
import type { RouteSettings } from "./ConnectionsSettings";

type Data = ReturnType<typeof useConnections>;
type Entry = { id: string; name: string; sub: string; provider?: Provider; kind?: "direct" | "custom"; routes: RouteProfile[] };

/** Providers whose account number is enough: the engine makes the key and the locations. */
const ACCOUNT_PROVIDERS = new Set(["mullvad"]);

export function ConnectionsTab({ data, activeId, onUse, settings, onSettingChange }: {
  data: Data; activeId: string; onUse: (r: RouteProfile) => void;
  settings: RouteSettings; onSettingChange: (key: keyof RouteSettings, value: string | boolean) => void;
}) {
  const [selected, setSelected] = useState<string>(() => {
    const active = data.routes.find((r) => r.id === activeId);
    return (active && providerOfRoute(active)) || "direct";
  });
  const [query, setQuery] = useState("");
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuEntry[]; label: string } | null>(null);
  const [showGuide, setShowGuide] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);

  const entries = useMemo(() => {
    const byProvider = new Map<string, RouteProfile[]>();
    for (const r of data.routes) {
      const p = providerOfRoute(r);
      if (p) byProvider.set(p, [...(byProvider.get(p) ?? []), r]);
    }
    const direct: Entry = { id: "direct", name: "Direct", sub: "No tunnel · your own connection", kind: "direct", routes: byProvider.get("direct") ?? [] };
    const mine: Entry[] = [];
    const rest: Entry[] = [];
    for (const provider of PROVIDERS) {
      const routes = byProvider.get(provider.id) ?? [];
      const e: Entry = { id: provider.id, name: provider.name, provider, routes, sub: routes.length ? `${routes.length} location${routes.length === 1 ? "" : "s"}` : provider.badge };
      (routes.length ? mine : rest).push(e);
    }
    const custom = byProvider.get("custom") ?? [];
    if (custom.length) mine.push({ id: "custom", name: "Other WireGuard", sub: `${custom.length} imported`, kind: "custom", routes: custom });
    const q = query.trim().toLowerCase();
    const match = (e: Entry) => !q || e.name.toLowerCase().includes(q);
    return { mine: [direct, ...mine].filter(match), rest: rest.filter(match) };
  }, [data.routes, query]);
  const flat = [...entries.mine, ...entries.rest];
  const current = flat.find((e) => e.id === selected) ?? entries.mine[0];

  const select = (id: string, focus = false) => {
    setSelected(id);
    setShowGuide(false);
    if (focus) requestAnimationFrame(() => listRef.current?.querySelector<HTMLElement>(`[data-entry="${id}"]`)?.focus());
  };

  const entryMenu = (e: Entry): MenuEntry[] => {
    if (e.kind === "direct") {
      const route = e.routes[0];
      return [
        { label: "Use for downloads", checked: activeId === route?.id || activeId === "auto", onSelect: () => route && onUse(route) },
        { label: "Check connection", icon: ic.refreshCw, onSelect: () => route && void data.check(route) },
      ];
    }
    if (!e.routes.length && e.provider) return [
      { label: `Set up ${e.name}`, icon: ic.plus, kbd: "Enter", onSelect: () => select(e.id) },
      ...(e.provider.guideUrl ? [{ label: "Open setup page", icon: ic.externalLink, onSelect: () => window.open(e.provider!.guideUrl, "_blank", "noopener") }] : []),
      ...(e.provider.githubToolUrl ? [{ label: `Open ${e.provider.githubToolLabel ?? "community tool"}`, icon: ic.externalLink, onSelect: () => window.open(e.provider!.githubToolUrl, "_blank", "noopener") }] : []),
    ];
    return [
      { label: "Check every location", icon: ic.refreshCw, onSelect: () => void data.checkMany(e.routes) },
      { label: "Use the next healthy connection", icon: ic.swap, onSelect: async () => {
        const next = await data.nextBest(activeId);
        if (next) { onUse(next); toast(`Downloads now use ${next.region || next.id}`, { tone: "ok" }); }
        else toast("No other healthy connection right now");
      } },
      "-",
      { label: "Import more locations…", icon: ic.upload, onSelect: () => { select(e.id); setShowGuide(true); } },
      ...(e.provider ? [{ label: "Show setup guide", icon: ic.book, onSelect: () => { select(e.id); setShowGuide(true); } }] : []),
      "-",
      { label: `Remove all ${e.routes.length} locations`, icon: ic.trash, danger: true, onSelect: () => data.remove(e.routes, e.name) },
    ];
  };

  const onListKey = (ev: React.KeyboardEvent) => {
    const i = flat.findIndex((x) => x.id === current?.id);
    const go = (to: number) => { ev.preventDefault(); select(flat[Math.max(0, Math.min(flat.length - 1, to))].id, true); };
    if (ev.key === "ArrowDown") go(i + 1);
    else if (ev.key === "ArrowUp") go(i - 1);
    else if (ev.key === "Home") go(0);
    else if (ev.key === "End") go(flat.length - 1);
    else if (isMenuKey(ev) && current) {
      ev.preventDefault();
      const el = listRef.current?.querySelector(`[data-entry="${current.id}"]`);
      if (el) { const at = menuAnchor(el); setMenu({ ...at, items: entryMenu(current), label: `Actions for ${current.name}` }); }
    }
  };

  const item = (e: Entry) => {
    const isActive = e.routes.some((r) => r.id === activeId) || (e.kind === "direct" && (activeId === "auto" || activeId === "direct"));
    return (
      <div
        key={e.id}
        data-entry={e.id}
        role="option"
        aria-selected={current?.id === e.id}
        tabIndex={current?.id === e.id ? 0 : -1}
        className={`cx-conn${current?.id === e.id ? " on" : ""}${e.routes.length || e.kind ? "" : " off"}`}
        onClick={() => select(e.id)}
        onContextMenu={(ev) => { ev.preventDefault(); select(e.id); setMenu({ x: ev.clientX, y: ev.clientY, items: entryMenu(e), label: `Actions for ${e.name}` }); }}
      >
        <ProviderLogo provider={e.provider} kind={e.kind} size={30} />
        <span className="cx-conn-text">
          <span className="cx-conn-name">{e.name}</span>
          <span className="cx-conn-sub">{e.sub}</span>
        </span>
        {isActive && <span className="ui-dot ok" title="Used for downloads" aria-label="Used for downloads" />}
      </div>
    );
  };

  return (
    <div className="cx-split">
      <aside className="cx-list" aria-label="Providers">
        <label className="cx-search">
          <Icon d={ic.search} size={12} />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find a provider" aria-label="Find a provider" />
        </label>
        <div role="listbox" aria-label="Connections" ref={listRef} onKeyDown={onListKey} className="cx-listbox">
          {entries.mine.length > 0 && <div className="cx-sec" role="presentation">Your connections</div>}
          {entries.mine.map(item)}
          {entries.rest.length > 0 && <div className="cx-sec" role="presentation">All providers · {entries.rest.length}</div>}
          {entries.rest.map(item)}
          {!flat.length && <p className="ui-empty">No provider matches. Any WireGuard provider works through “Other WireGuard” — import its .conf.</p>}
        </div>
        <button type="button" className="cx-conn cx-import" onClick={() => { select("custom"); setShowGuide(true); }}>
          <ProviderLogo kind="import" size={30} />
          <span className="cx-conn-text"><span className="cx-conn-name">Import WireGuard .conf</span><span className="cx-conn-sub">Any provider</span></span>
        </button>
      </aside>

      <div className="cx-detail">
        {current && (current.routes.length && !showGuide ? (
          <ProviderLocations
            entry={current}
            provider={current.provider}
            data={data}
            activeId={activeId}
            settings={settings}
            onUse={onUse}
            onSettingChange={onSettingChange}
            onImportMore={() => setShowGuide(true)}
            onMenu={(x, y, items, label) => setMenu({ x, y, items, label })}
          />
        ) : (
          <ProviderSetup
            provider={current.provider ?? providerById(current.id)}
            custom={current.id === "custom" || current.kind === "custom"}
            onImport={async (conf, location) => {
              const route = await data.importWireguard(current.provider?.id ?? "custom", conf, location);
              setShowGuide(false);
              toast(`Imported ${location}`, { tone: "ok" });
              if (route) void data.check(route);
            }}
            onBack={current.routes.length ? () => setShowGuide(false) : undefined}
            account={ACCOUNT_PROVIDERS.has(current.id) ? {
              locations: current.routes.filter((r) => r.peer_public_key).length,
              onSignIn: async (n) => { await data.account("signIn", current.id, n); setShowGuide(false); toast(`Signed in to ${current.name}`, { tone: "ok" }); },
              onRefresh: async () => { await data.account("refresh", current.id); toast("Server list updated", { tone: "ok" }); },
              onSignOut: async () => { await data.account("signOut", current.id); toast(`Signed out of ${current.name}`); },
            } : undefined}
          />
        ))}
      </div>

      {menu && <ActionMenu x={menu.x} y={menu.y} items={menu.items} label={menu.label} onClose={() => setMenu(null)} />}
    </div>
  );
}
