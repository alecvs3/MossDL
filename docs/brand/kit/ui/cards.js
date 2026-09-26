// Floating cards for the brand graphics: small, legible pieces of the real UI
// (an Explore tree, a connection, the follow browser, the captcha handoff).
(() => {
  const { esc, fmt, cell, header, cols, siteIcon } = K;
  const themeAttr = (t) => (t && t !== "midnight" ? ` data-theme="${t}"` : "");

  /** Wraps content in a positioned card with its own theme and zoom. */
  K.card = ({ x, y, w, h, zoom = 1, theme, inner, style = "" }) =>
    `<div class="card" style="left:${x}px;top:${y}px;width:${w}px;${h ? `height:${h}px;` : ""}${style}">
      <div class="figma-shell mk-shell"${themeAttr(theme)} style="zoom:${zoom};width:${w / zoom}px;${h ? `height:${h / zoom}px` : ""}">${inner}</div></div>`;

  const CARD_DEFS = [["name", "", null, "minmax(0,1fr)"], ["size", "", "flex-end", "66px"], ["match", "", null, "88px"]];
  /** The Explore tree for opencinema.org, trimmed to a card. */
  K.exploreTree = (o = {}) => {
    const X = KIT_DATA.explore;
    const rows = [
      { id: "site", type: "source", label: X.site, host: X.site, open: true, kids: true, chip: "2 pages · 7 files" },
      { id: "g1", type: "group", depth: 1, label: "Download buttons", open: true, kids: true, chip: "3" },
      { id: "m1", type: "button", depth: 2, label: X.buttons[0].label, follow: true, tag: o.followed ? { tone: "ok", text: "3 steps" } : null, kids: !!o.followed, open: !!o.followed },
      ...(o.followed ? [{ id: "ff", type: "file", depth: 3, label: X.followed.name, kind: "video", size: X.followed.size, fresh: true, queued: o.queued }] : []),
      { id: "m3", type: "link", depth: 2, label: X.buttons[2].label, sub: X.buttons[2].sub },
      ...(o.files === 0 ? [] : [{ id: "g2", type: "group", depth: 1, label: "Files", open: true, kids: true, chip: "5" }]),
      ...X.files.slice(0, o.files ?? 3).map((f) => ({ ...f, type: "file", depth: 2, label: f.name, check: f.best ? "on" : "off" })),
    ];
    const defs = o.match === false ? CARD_DEFS.slice(0, 2) : CARD_DEFS;
    return `<div class="dt-scroll xp-table mk-cardtable" style="${cols(defs.map((d) => d[3]))}"><div class="dt-content">${rows.map((r) => K.exploreRow(r, { defs })).join("")}</div></div>`;
  };

  K.exploreCard = (o) => K.card({ ...o, inner: `<div class="card-head">${icon("globe", 13)} Explore <span class="sub">· ${KIT_DATA.explore.site}${KIT_DATA.explore.page}</span><span class="grow"></span><span class="xp-count-chip">7 files</span></div>${K.exploreTree(o)}` });

  const LOC_DEFS = [["status", "", "center", "22px"], ["name", "", null, "minmax(0,1fr)"], ["latency", "", "flex-end", "50px"]];
  /** Mullvad locations with the one in use, plus what the engine measured for it. */
  K.connectionCard = (o) => {
    const active = o.active || "sto";
    const locs = KIT_DATA.locations.filter((l) => (o.only || ["ams", "fra", "sto", "zrh"]).includes(l.id));
    const a = KIT_DATA.locations.find((l) => l.id === active);
    const rows = locs.map((l) => {
      const on = l.id === active;
      const cells = { status: `<span class="cx-radio${on ? " on" : ""}"></span>`,
        name: `<span class="cx-cell-name"><span class="ui-dot ok"></span><span>${l.name}</span>${on ? '<span class="ui-chip ok">in use</span>' : ""}</span>`,
        latency: `<span class="mono good">${l.ms} ms</span>` };
      return `<div class="dt-row cx-row${on ? " picked current" : ""}">${LOC_DEFS.map(([id, , al]) => cell(id, cells[id], al)).join("")}</div>`;
    }).join("");
    return K.card({ ...o, inner: `<div class="card-head">${K.providerLogo("mullvad", 18)} Mullvad VPN <span class="sub">· WireGuard</span><span class="grow"></span><span class="ui-dot ok"></span></div>
      <div class="dt-scroll cx-table mk-cardtable" style="${cols(LOC_DEFS.map((d) => d[3]))}"><div class="dt-content">${rows}</div></div>
      <dl class="cx-facts mk-cardfacts"><dt>Exit IP</dt><dd class="mono">${a.ip}</dd><dt>Runs in</dt><dd>MossDL (built-in tunnel)</dd><dt>Handshake</dt><dd>3 s ago</dd></dl>` });
  };

  /** The background browser following Mirror 1: a countdown with ads blocked. */
  K.browserCard = (o) => {
    const n = o.count ?? 7;
    const ad = (w, h) => `<div class="bw-ad" style="width:${w};height:${h}px">${icon("ban", 13)}<span>Ad blocked</span></div>`;
    return K.card({ ...o, inner: `<div class="bw-bar"><span class="bw-dots"><i></i><i></i><i></i></span><span class="bw-addr">https://filemirror.net/f/8Kq2/sintel-2010</span><span class="bw-tag">${icon("shield", 11)} ${o.blocked ?? 3} blocked</span></div>
      <div class="bw-page">${ad("100%", 34)}
        <div class="bw-file">${K.kindTile("video", 30)}<div><b>Sintel.2010.1080p.mkv</b><span>${fmt(1223)}</span></div></div>
        <div class="bw-go"><div class="bw-count"><span class="bw-ring" style="--p:${(n / 15) * 100}"><b>${n}</b></span><span>${n > 0 ? "Your link is ready in" : "Your link is ready"}<br><small>${n > 0 ? `${n} seconds` : "Click to download"}</small></span></div>
        <div class="bw-btn${n > 0 ? " off" : ""}${o.press ? " mk-press" : ""}">${icon("download", 13)} ${n > 0 ? "Please wait…" : "Get link"}</div></div>
        ${o.compact ? "" : `<div class="bw-ads">${ad("48%", 52)}${ad("48%", 52)}</div>`}</div>` });
  };

  /** The Explore filter column on its own card. */
  K.filtersCard = (o) => K.card({ ...o, inner: `<div class="mk-filtercard">${K.exploreFilters(K.sintelCounts, o.sections)}</div>` });

  /** "Solve in browser": the challenge goes to your browser and the answer comes back verified. */
  K.handoffCard = (o) => {
    const step = (ic, title, sub, tone) => `<div class="ho-step ${tone || ""}"><span class="ho-ic">${icon(ic, 15)}</span><div><b>${title}</b><span>${sub}</span></div></div>`;
    return K.card({ ...o, inner: `<div class="card-head">${icon("externalLink", 13)} Solve in browser <span class="grow"></span><span class="ui-chip ok">verified</span></div>
      <div class="ho-flow">${step("captchas", "Challenge opens in your browser", "Only the site's own page, with a single-use ticket")}
        ${step("check", "You answer it", "The MossDL extension picks up the result")}
        ${step("shield", "The answer comes back", "Bound to this download, origin and profile")}
        ${step("play", "The site accepts it", "The download resumes", "ok")}</div>` });
  };
})();
