// Connections: VPN locations, proxies and switching rules on one page.
// Replaces the old multi-tab Routes/VPN page.
import React, { useEffect, useMemo, useState } from "react";
import type { RouteProfile } from "../../../api";
import { useConnections } from "./useConnections";
import { ConnectionsTab } from "./ConnectionsTab";
import { ProxiesTab } from "./ProxiesTab";
import { ConnectionsSettings, type RouteSettings } from "./ConnectionsSettings";
import { isProxyRoute, routeLabel } from "./providers";
import { PageTabs, type PageTab } from "../../ui/PageTabs";
import "./connections.css";

type Tab = "connections" | "proxies" | "settings";
const TABS: PageTab<Tab>[] = [
  { id: "connections", label: "Connections" },
  { id: "proxies", label: "Proxies" },
  { id: "settings", label: "Settings" },
];


export function ConnectionsPage({ routes: initialRoutes, settings, onSettingChange }: {
  routes: RouteProfile[];
  settings: RouteSettings;
  onSettingChange: (key: keyof RouteSettings, value: string | boolean) => void;
}) {
  const data = useConnections(initialRoutes);
  const [tab, setTab] = useState<Tab>("connections");
  const proxies = useMemo(() => data.routes.filter(isProxyRoute), [data.routes]);
  const active = data.routes.find((r) => r.id === settings.activeLocation);
  const activeHealth = active ? data.health[active.id] : undefined;

  // Check the route in use whenever it changes, so its status is real.
  useEffect(() => { if (active && !data.health[active.id]) void data.check(active); }, [active?.id]); // eslint-disable-line react-hooks/exhaustive-deps




  const useRoute = (route: RouteProfile) => onSettingChange("activeLocation", route.id);

  return (
    <div className="page-shell">
      <div className="page-head">
        <PageTabs label="Connections" idPrefix="cx" value={tab} onChange={setTab}
          tabs={TABS.map((t) => (t.id === "proxies" ? { ...t, count: proxies.length } : t))} />
        <span className="grow" />
        <span className="cx-now" title={activeHealth?.error ?? undefined}>
          <span className={`ui-dot ${!active || active.kind === "direct" ? "muted" : activeHealth?.checking ? "busy" : activeHealth?.healthy ? "ok" : activeHealth ? "bad" : "muted"}`} aria-hidden="true" />
          Downloads use <b>{routeLabel(active)}</b>
        </span>
        <span className="cx-sep" aria-hidden="true" />
        <button
          type="button"
          role="switch"
          aria-checked={settings.autoSwitchOnQuota}
          className="head-switch"
          title="When a host's download limit is hit, move the download to another connection"
          onClick={() => onSettingChange("autoSwitchOnQuota", !settings.autoSwitchOnQuota)}
        >
          Auto-switch on quota <span className={`mini-switch${settings.autoSwitchOnQuota ? " on" : ""}`} aria-hidden="true" />
        </button>
      </div>

      <section id="cx-panel-connections" role="tabpanel" aria-labelledby="cx-tab-connections" hidden={tab !== "connections"} className="page-panel">
        {tab === "connections" && <ConnectionsTab data={data} activeId={settings.activeLocation} onUse={useRoute} settings={settings} onSettingChange={onSettingChange} />}
      </section>
      <section id="cx-panel-proxies" role="tabpanel" aria-labelledby="cx-tab-proxies" hidden={tab !== "proxies"} className="page-panel">
        {tab === "proxies" && <ProxiesTab data={data} proxies={proxies} activeId={settings.activeLocation} onUse={useRoute} />}
      </section>
      <section id="cx-panel-settings" role="tabpanel" aria-labelledby="cx-tab-settings" hidden={tab !== "settings"} className="page-panel page-scroll">
        {tab === "settings" && <ConnectionsSettings settings={settings} routes={data.routes} onChange={onSettingChange} />}
      </section>
    </div>
  );
}
