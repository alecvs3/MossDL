// Downloads page: header, grouped table of DownloadRow and the status bar
// (pages/DownloadsPage.tsx, components/DownloadRow.tsx, DownloadsStatusBar.tsx).
(() => {
  const { esc, fmt, kindTile, statusBadge, progressCell, sizeCell, hostCell, sparkline, eta, cell, header, cols } = K;

  const ACCENT = { downloading: "var(--accent-light)", paused: "var(--warning)", failed: "var(--danger)", completed: "var(--success)", resolving: "#38bdf8" };
  const ALL = [
    ["select", "", "center", "36px"], ["name", "File", null, "minmax(180px,1fr)"], ["status", "Status", null, "118px"],
    ["host", "Host", null, "136px"], ["speed", "Speed", null, "112px"], ["eta", "ETA", "flex-end", "52px"],
    ["pct", "Progress", null, "124px"], ["size", "Size", "flex-end", "128px"], ["actions", "", "flex-end", "52px"],
  ];

  function row(d, o) {
    const live = d.status === "downloading";
    const p = d.status === "completed" ? 100 : d.size ? (d.done / d.size) * 100 : 0;
    const accent = ACCENT[d.status];
    const fill = d.status === "paused" ? "background:rgba(200,140,30,0.5)" : d.status === "failed" ? "background:rgba(220,80,80,0.55)" : "";
    const cells = {
      select: `<span class="download-select-trigger${d.selected ? " selected" : ""}">${d.selected ? icon("check", 12) : kindTile(d.kind, 22)}</span>`,
      name: `<div class="mk-namecell"><p class="mk-name">${esc(d.name)}</p><p class="mk-sub">${esc(d.via || "Direct")} · ${esc(d.added || "just now")}</p></div>`,
      status: statusBadge(d.badge || d.status, d.badgeText),
      host: hostCell(d.host),
      speed: live && d.speed ? sparkline(d.speed, d.seed ?? d.name.length, o.t || 0) : `<span class="mk-mono9" style="color:var(--ink-20)">${d.status === "paused" ? "Paused" : "—"}</span>`,
      eta: `<span class="mk-mono9" style="color:var(--ink-30)">${live ? eta(d.size - d.done, d.speed) : "—"}</span>`,
      pct: progressCell(p, d.status),
      size: sizeCell(d.done, d.size, d.status),
      actions: `<div class="mk-rowacts${o.hover === d.id ? " on" : ""}">${live || d.status === "paused" || d.status === "queued"
        ? `<span class="row-action-btn row-action-btn-primary">${icon(live ? "pause" : "play", 10)}</span>` : ""}<span class="row-action-btn row-action-btn-danger">${icon("trash", 10)}</span></div>`,
    };
    const style = d.enter != null ? ` style="opacity:${d.enter};transform:translateY(${(1 - d.enter) * -6}px)"` : "";
    return `<div data-k="${d.id}" class="dl-row${d.selected ? " selected" : ""}${o.focus === d.id ? " focused" : ""}${d.flash ? " mk-flash" : ""}"${style}>
      ${accent ? `<div class="dl-row-accent" style="background:${accent};box-shadow:0 0 4px ${accent}"></div>` : ""}
      <div class="dl-row-progress"><div class="dl-row-progress-fill${live ? " progress-shimmer" : ""}" style="width:${p}%;${fill}"></div></div>
      ${o.defs.map(([id, , align]) => cell(id, cells[id], align)).join("")}
    </div>`;
  }

  const GROUPS = [
    ["Transferring", "zap", (d) => ["downloading", "resolving"].includes(d.status)],
    ["Waiting", "pause", (d) => ["paused", "queued"].includes(d.status)],
    ["Completed", "check", (d) => d.status === "completed"],
  ];
  function groupHeader(label, ic, list) {
    const c = label === "Transferring" ? "var(--accent-light)" : "var(--ink-40)";
    const speed = list.reduce((s, d) => s + (d.status === "downloading" ? d.speed : 0), 0);
    return `<div class="dl-group-header mk-group"><span style="color:${c}">${icon(ic, 11)}</span><span class="mk-gl" style="color:${c}">${label}</span><span class="mk-gc">${list.length}</span>${speed ? `<span class="mk-gs">↓ ${speed.toFixed(1)} MB/s</span>` : ""}</div>`;
  }

  function statusBar(list, selected) {
    const n = (f) => list.filter(f).length;
    const active = list.filter((d) => d.status === "downloading");
    const parts = [[active.length, "downloading", "active"], [n((d) => d.status === "queued"), "queued"], [n((d) => d.status === "paused"), "paused"], [n((d) => d.status === "completed"), "completed"]];
    const speed = active.reduce((s, d) => s + d.speed, 0);
    const total = list.reduce((s, d) => s + d.size, 0);
    return `<div class="dl-status"><span>${list.length} items${selected ? ` (${selected} selected)` : ""}</span>${parts.filter(([c]) => c).map(([c, l, tone]) =>
      `<span${tone ? ` class="dl-status-${tone}"` : ""}>${c} ${l}</span>`).join("")}<span>${fmt(total)} total</span><span class="grow"></span><span class="dl-status-speed">${icon("download", 11)} ${speed ? `${speed.toFixed(1)} MB/s` : "idle"}</span></div>`;
  }

  /** opts: list, t (seconds, drives sparklines), columns (ids to show), focus, hover, grouped, statusbar */
  K.downloadsPage = ({ list, t = 0, columns, focus, hover, grouped = true, statusbar = true, filter = "", bare = false }) => {
    const defs = K.pick(ALL, columns);
    const o = { defs, t, focus, hover };
    const active = list.filter((d) => d.status === "downloading");
    const speed = active.reduce((s, d) => s + d.speed, 0);
    const body = grouped
      ? GROUPS.map(([label, ic, f]) => { const g = list.filter(f); return g.length ? groupHeader(label, ic, g) + g.map((d) => row(d, o)).join("") : ""; }).join("")
      : list.map((d) => row(d, o)).join("");
    if (bare) return `<div class="dt-scroll mk-dltable" style="${cols(defs.map((d) => d[3]))}"><div class="dt-content"><div class="mk-rows">${body}</div></div></div>`;
    return `<div class="mk-dlhead"><div class="mk-dlhead-l"><h1>Downloads</h1>${active.length ? `<span class="mk-active">${active.length} active</span><span class="mk-total">↓ ${speed.toFixed(1)} MB/s</span>` : ""}</div>
        <label class="win-input mk-filter">${icon("search", 11)}<span>${filter ? esc(filter) : "Filter…"}</span></label></div>
      <div class="dt-scroll mk-dltable" style="${cols(defs.map((d) => d[3]))}"><div class="dt-content">${header(defs.map(([id, title, align]) => [id, title, align]))}<div class="mk-rows">${body}</div></div></div>
      ${statusbar ? statusBar(list, list.filter((d) => d.selected).length) : ""}`;
  };
})();
