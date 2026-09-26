// Connections page: provider list, a provider's locations and the connection
// panel (pages/routes/ConnectionsPage.tsx, ConnectionsTab.tsx,
// ProviderLocations.tsx, ConnectionPanel.tsx).
(() => {
  const { SRC, esc, cell, header, cols } = K;
  const DEFS = [["status", "", "center", "30px"], ["name", "Location", null, "minmax(150px,1fr)"], ["ip", "Exit IP", null, "124px"], ["latency", "Latency", "flex-end", "76px"], ["actions", "", "flex-end", "36px"]];
  const latencyClass = (ms) => (ms == null ? "" : ms < 80 ? "good" : ms < 200 ? "fair" : "slow");

  const logo = (id, size) => {
    const r = Math.round(size * 0.24);
    if (id === "direct" || id === "custom" || id === "import") {
      const ic = id === "direct" ? "globe" : id === "import" ? "upload" : "shield";
      return `<span class="app-tile glyph" style="width:${size}px;height:${size}px;border-radius:${r}px">${icon(ic, Math.round(size * 0.5))}</span>`;
    }
    return `<span class="app-tile glyph" style="width:${size}px;height:${size}px;border-radius:${r}px"><img src="${SRC}figma/assets/vpn/${id}.png" alt="" style="width:72%;height:72%"></span>`;
  };
  K.providerLogo = logo;

  function list(activeId) {
    const conn = (id, name, sub, on, active, off) => `<div class="cx-conn${on ? " on" : ""}${off ? " off" : ""}">${logo(id, 30)}<span class="cx-conn-text"><span class="cx-conn-name">${name}</span><span class="cx-conn-sub">${sub}</span></span>${active ? '<span class="ui-dot ok"></span>' : ""}</div>`;
    return `<aside class="cx-list">
      <label class="cx-search">${icon("search", 12)}<span class="mk-ph">Find a provider</span></label>
      <div class="cx-listbox">
        <div class="cx-sec">Your connections</div>
        ${conn("direct", "Direct", "No tunnel · your own connection", false, !activeId)}
        ${conn("mullvad", "Mullvad VPN", "8 locations", true, !!activeId)}
        <div class="cx-sec">All providers · 13</div>
        ${conn("proton", "Proton VPN", "WireGuard", false, false, true)}
        ${conn("ivpn", "IVPN", "WireGuard", false, false, true)}
        ${conn("airvpn", "AirVPN", "WireGuard", false, false, true)}
        ${conn("windscribe", "Windscribe", "WireGuard", false, false, true)}
      </div>
      <div class="cx-conn cx-import">${logo("import", 30)}<span class="cx-conn-text"><span class="cx-conn-name">Import WireGuard .conf</span><span class="cx-conn-sub">Any provider</span></span></div>
    </aside>`;
  }

  function side(o, loc) {
    const active = K_DATA_LOC(o.active);
    const state = o.checking ? ["busy", "Checking…"] : active ? ["ok", "Tunnel up"] : ["muted", "Online"];
    const inUse = active && loc && active.id === loc.id;
    const toggle = (label, on) => `<div class="cx-panel-toggle"><span>${label}</span><span class="mini-switch${on ? " on" : ""}"></span></div>`;
    return `<aside class="cx-side">
      <section><div class="cx-side-head"><h3>Connection</h3><span class="cx-side-state"><span class="ui-dot ${state[0]}"></span>${active ? `${active.name} · ${state[1]}` : "Direct · Online"}</span></div>
        <button class="btn-accent cx-side-primary${o.press === "connect" ? " mk-press" : ""}"${inUse ? " disabled" : ""}>${icon("play", 12)} ${inUse ? "In use" : `Connect to ${loc ? loc.name : "—"}`}</button>
        <div class="cx-side-row"><button class="ui-btn"${active ? "" : " disabled"}>${icon("power", 12)} Disconnect</button><button class="ui-btn">${icon("swap", 12)} Next best</button></div>
        <button class="ui-btn cx-side-wide">${icon("refreshCw", 12)} Check all servers</button></section>
      <section><div class="cx-side-head"><h3>Selected server</h3><span class="ui-link">Check now</span></div>
        <dl class="cx-facts"><dt>Location</dt><dd>${loc ? loc.name : "—"}</dd>
          <dt>Status</dt><dd><span class="ui-dot ${inUse ? state[0] : "ok"}"></span> ${inUse ? state[1] : "Tunnel up"}</dd>
          <dt>Exit IP</dt><dd class="mono">${loc ? loc.ip : "—"}</dd>
          <dt>Latency</dt><dd class="${latencyClass(loc && loc.ms)}">${loc ? loc.ms + " ms" : "—"}</dd>
          <dt>Runs in</dt><dd>MossDL (built-in tunnel)</dd>
          ${inUse && !o.checking ? `<dt>Handshake</dt><dd>${o.handshake || "3 s ago"}</dd><dt>Traffic</dt><dd>${o.traffic || "↓ 412 MB · ↑ 3.1 MB"}</dd>` : ""}
        </dl></section>
      <section><div class="cx-side-head"><h3>Switching</h3></div>${toggle("Auto-switch on quota", true)}${toggle("Include proxies", false)}${toggle("Allow Direct fallback", false)}</section>
    </aside>`;
  }
  const K_DATA_LOC = (id) => KIT_DATA.locations.find((l) => l.id === id);

  /** opts: active (location id or null for Direct), picked, checking, hover, press, side (show panel) */
  K.connectionsPage = (o = {}) => {
    const defs = K.pick(DEFS, o.columns);
    const locs = KIT_DATA.locations;
    const loc = K_DATA_LOC(o.picked) || K_DATA_LOC(o.active);
    const active = K_DATA_LOC(o.active);
    const rows = locs.map((l) => {
      const on = loc && loc.id === l.id, inUse = active && active.id === l.id;
      const dot = inUse && o.checking ? "busy" : "ok";
      const cells = {
        status: `<span class="cx-radio${on ? " on" : ""}"></span>`,
        name: `<span class="cx-cell-name"><span class="ui-dot ${dot}"></span><span class="mk-trunc">${esc(l.name)}</span>${inUse && !o.checking ? '<span class="ui-chip ok">in use</span>' : ""}</span>`,
        ip: `<span class="mono cx-muted">${l.ip}</span>`,
        latency: `<span class="mono ${latencyClass(l.ms)}">${l.ms} ms</span>`,
        actions: `<span class="cx-icon">${icon("dots", 12)}</span>`,
      };
      return `<div data-k="${l.id}" class="dt-row cx-row${on ? " picked" : ""}${inUse ? " current" : ""}${o.hover === l.id ? " mk-hover" : ""}">${defs.map(([id, , a]) => cell(id, cells[id], a)).join("")}</div>`;
    }).join("");
    const now = `<span class="cx-now"><span class="ui-dot ${active ? (o.checking ? "busy" : "ok") : "muted"}"></span>Downloads use <b>${active ? `Mullvad VPN · ${active.name}` : "Direct"}</b></span>${o.headSwitch === false ? "" : '<span class="cx-sep"></span><span class="head-switch">Auto-switch on quota <span class="mini-switch on"></span></span>'}`;
    return `<div class="page-shell">${K.pageHead([["connections", "Connections"], ["proxies", "Proxies", 0], ["settings", "Settings"]], "connections", now)}
      <div class="cx-split${o.noList ? " mk-nolist" : ""}">${o.noList ? "" : list(o.active)}<div class="cx-detail"><div class="cx-locations-3${o.side === false ? " mk-noside" : ""}"><div class="cx-locations">
        <div class="cx-detail-top">${logo("mullvad", 40)}<div class="cx-detail-title"><h2>Mullvad VPN</h2><div class="ui-chips"><span class="ui-chip ok">One key works for every location</span></div></div>
          <span class="cx-pill">${icon("mapPin", 11)} ${locs.length} locations</span></div>
        <div class="dt-scroll cx-table" style="${cols(defs.map((d) => d[3]))}"><div class="dt-content">${header(defs)}${rows}</div></div>
      </div>${o.side === false ? "" : side(o, loc)}</div></div></div></div>`;
  };
  K.connectionSide = (o) => side(o, K_DATA_LOC(o.picked) || K_DATA_LOC(o.active));
})();
