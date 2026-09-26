// Connections settings. Every option here is read by the engine
// (engine/service.py quota handling and route selection).
import React from "react";
import type { RouteProfile } from "../../../api";
import { routeLabel } from "./providers";

export interface RouteSettings {
  activeLocation: string;
  autoSwitchOnQuota: boolean;
  switchIncludesProxies: boolean;
  allowDirectFallback: boolean;
}

function Check({ checked, label, description, onChange, sub }: {
  checked: boolean; label: string; description?: string; onChange: (v: boolean) => void; sub?: boolean;
}) {
  const id = `cx-set-${label.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <div
      className={`cx-set-row check${sub ? " sub" : ""}`}
      role="checkbox"
      aria-checked={checked}
      aria-labelledby={id}
      aria-describedby={description ? `${id}-d` : undefined}
      tabIndex={0}
      onClick={() => onChange(!checked)}
      onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); onChange(!checked); } }}
    >
      <span className={`cx-check${checked ? " on" : ""}`} aria-hidden="true" />
      <div className="cx-set-text">
        <div id={id} className="cx-set-label">{label}</div>
        {description && <div id={`${id}-d`} className="cx-set-desc">{description}</div>}
      </div>
    </div>
  );
}

export function ConnectionsSettings({ settings, routes, onChange }: {
  settings: RouteSettings;
  routes: RouteProfile[];
  onChange: (key: keyof RouteSettings, value: string | boolean) => void;
}) {
  const usable = routes.filter((r) => r.enabled !== false);
  return (
    <div className="cx-settings">
      <section className="cx-set-group" aria-labelledby="cx-set-switch">
        <h3 id="cx-set-switch">Automatic switching</h3>
        <div className="cx-set-card">
          <Check
            checked={settings.autoSwitchOnQuota}
            label="Auto-switch VPN / proxy when a host quota is hit"
            description="“Daily limit reached”, “wait 2 hours” and similar. The download moves to the next healthy connection and resumes."
            onChange={(v) => onChange("autoSwitchOnQuota", v)}
          />
          <Check
            sub
            checked={settings.switchIncludesProxies}
            label="Include proxies when switching"
            onChange={(v) => onChange("switchIncludesProxies", v)}
          />
          <Check
            sub
            checked={settings.allowDirectFallback}
            label="Allow falling back to Direct"
            description="Turn off to keep your real IP hidden: downloads wait for a VPN or proxy instead of using your own connection."
            onChange={(v) => onChange("allowDirectFallback", v)}
          />
        </div>
      </section>

      <section className="cx-set-group" aria-labelledby="cx-set-default">
        <h3 id="cx-set-default">New downloads</h3>
        <div className="cx-set-card">
          <label className="cx-set-row">
            <div className="cx-set-text">
              <div className="cx-set-label">Connection for new downloads</div>
              <div className="cx-set-desc">Also set from any location or proxy with “Use for downloads”.</div>
            </div>
            <select className="cx-select" value={settings.activeLocation} onChange={(e) => onChange("activeLocation", e.target.value)}>
              {!usable.some((r) => r.id === settings.activeLocation) && <option value={settings.activeLocation}>{settings.activeLocation === "auto" ? "Direct" : settings.activeLocation}</option>}
              {usable.map((r) => <option key={r.id} value={r.id}>{routeLabel(r)}</option>)}
            </select>
          </label>
        </div>
      </section>

      <p className="cx-set-note">
        WireGuard locations run inside Transfer: no WireGuard app, driver or admin rights. Downloads, page
        crawls, site icons and the browser all go through the selected location, and if its tunnel drops they
        stop rather than continue on your own connection.
      </p>
    </div>
  );
}
