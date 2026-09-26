// Right-hand panel of a provider's locations: act on the picked server, see
// what the engine measured for it, and the switching rules. Only values the
// engine reports are shown; nothing here is estimated.
import React from "react";
import type { RouteProfile } from "../../../api";
import { formatBytes } from "../../../lib/format";
import { Icon, ic } from "../../icons";
import type { RouteSettings } from "./ConnectionsSettings";
import type { Health } from "./useConnections";

const ago = (ms?: number) => {
  if (!ms) return "never";
  const s = Math.round((Date.now() - ms) / 1000);
  return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
};
/** Where a WireGuard location's tunnel runs: inside Transfer when its keys were imported. */
const runsIn = (r: RouteProfile) =>
  r.credential_ref && r.tunnel !== "system" ? "Transfer (built-in tunnel)" : "WireGuard app — re-import the .conf to run it in Transfer";

export const latencyClass = (ms?: number | null) => (ms == null ? "" : ms < 80 ? "good" : ms < 200 ? "fair" : "slow");
export const healthDot = (h?: Health) => (h?.checking ? "busy" : !h ? "muted" : h.healthy ? "ok" : "bad");
export const statusText = (h: Health | undefined, direct: boolean) =>
  h?.checking ? "Checking…" : !h ? "Not checked" : h.healthy ? (direct ? "Online" : "Tunnel up") : "Down";

function Toggle({ on, label, hint, onChange }: { on: boolean; label: string; hint?: string; onChange: (v: boolean) => void }) {
  return (
    <button type="button" role="switch" aria-checked={on} className="cx-panel-toggle" title={hint} onClick={() => onChange(!on)}>
      <span>{label}</span>
      <span className={`mini-switch${on ? " on" : ""}`} aria-hidden="true" />
    </button>
  );
}

export function ConnectionPanel({ picked, pickedHealth, active, activeHealth, inUse, canDisconnect, settings,
  onConnect, onDisconnect, onNextBest, onCheckAll, onCheckPicked, onSettingChange }: {
  picked?: RouteProfile;
  pickedHealth?: Health;
  active?: RouteProfile;
  activeHealth?: Health;
  inUse: boolean;
  canDisconnect: boolean;
  settings: RouteSettings;
  onConnect: () => void;
  onDisconnect: () => void;
  onNextBest: () => void;
  onCheckAll: () => void;
  onCheckPicked: () => void;
  onSettingChange: (key: keyof RouteSettings, value: string | boolean) => void;
}) {
  const direct = picked?.kind === "direct";
  const name = (r?: RouteProfile) => (r ? r.region || (r.kind === "direct" ? "Direct" : r.id) : "—");
  return (
    <aside className="cx-side" aria-label="Connection">
      <section>
        <div className="cx-side-head">
          <h3>Connection</h3>
          <span className="cx-side-state">
            <span className={`ui-dot ${active ? healthDot(activeHealth) : "muted"}`} aria-hidden="true" />
            {active ? `${name(active)} · ${statusText(activeHealth, active.kind === "direct")}` : "Direct"}
          </span>
        </div>
        <button type="button" className="btn-accent cx-side-primary" disabled={!picked || inUse} onClick={onConnect}
          title="Send new downloads through the selected server">
          <Icon d={ic.play} size={12} /> {inUse ? "In use" : `Connect to ${name(picked)}`}
        </button>
        <div className="cx-side-row">
          <button type="button" className="ui-btn" disabled={!canDisconnect} onClick={onDisconnect} title="Go back to your own connection">
            <Icon d={ic.power} size={12} /> Disconnect
          </button>
          <button type="button" className="ui-btn" onClick={onNextBest} title="The engine picks the next healthy connection">
            <Icon d={ic.swap} size={12} /> Next best
          </button>
        </div>
        <button type="button" className="ui-btn cx-side-wide" onClick={onCheckAll}><Icon d={ic.refreshCw} size={12} /> Check all servers</button>
      </section>

      <section aria-labelledby="cx-side-sel">
        <div className="cx-side-head"><h3 id="cx-side-sel">Selected server</h3>
          <button type="button" className="ui-link" disabled={!picked} onClick={onCheckPicked}>Check now</button></div>
        {picked ? (
          <dl className="cx-facts">
            <dt>Location</dt><dd>{name(picked)}</dd>
            {picked.endpoint && <><dt>Server</dt><dd className="mono">{picked.endpoint}</dd></>}
            <dt>Status</dt><dd><span className={`ui-dot ${healthDot(pickedHealth)}`} aria-hidden="true" /> {statusText(pickedHealth, direct)}</dd>
            <dt>Exit IP</dt><dd className="mono">{pickedHealth?.ip ?? "—"}</dd>
            <dt>Latency</dt><dd className={latencyClass(pickedHealth?.latencyMs)}>{pickedHealth?.latencyMs != null ? `${pickedHealth.latencyMs} ms` : "—"}</dd>
            <dt>Checked</dt><dd>{ago(pickedHealth?.checkedAt)}</dd>
            {picked.kind === "wireguard" && <><dt>Runs in</dt><dd>{runsIn(picked)}</dd></>}
            {pickedHealth?.tunnel && <>
              <dt>Handshake</dt><dd>{pickedHealth.tunnel.handshake_age != null ? `${Math.round(pickedHealth.tunnel.handshake_age)} s ago` : "—"}</dd>
              <dt>Traffic</dt><dd>↓ {formatBytes(pickedHealth.tunnel.rx_bytes ?? 0)} · ↑ {formatBytes(pickedHealth.tunnel.tx_bytes ?? 0)}</dd>
            </>}
            {pickedHealth?.error && !pickedHealth.healthy && <><dt>Problem</dt><dd className="cx-err">{pickedHealth.error}</dd></>}
          </dl>
        ) : <p className="cx-set-note">Pick a server in the list.</p>}
      </section>

      <section aria-labelledby="cx-side-sw">
        <div className="cx-side-head"><h3 id="cx-side-sw">Switching</h3></div>
        <Toggle on={settings.autoSwitchOnQuota} label="Auto-switch on quota" hint="Move a download to another connection when a host's limit is hit"
          onChange={(v) => onSettingChange("autoSwitchOnQuota", v)} />
        <Toggle on={settings.switchIncludesProxies} label="Include proxies" onChange={(v) => onSettingChange("switchIncludesProxies", v)} />
        <Toggle on={settings.allowDirectFallback} label="Allow Direct fallback" hint="Off keeps your real IP hidden"
          onChange={(v) => onSettingChange("allowDirectFallback", v)} />
      </section>
    </aside>
  );
}
