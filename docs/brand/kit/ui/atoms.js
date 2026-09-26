// Small building blocks shared by every mock page. Each mirrors a component in
// src/figma/ui (KindTile, SiteIcon, StatusBadge, DownloadCells, Sparkline) and
// uses the app's own class names, so src/figma/index.css styles them.
(() => {
  const SRC = "../../../src/";
  const BUNDLED = { "archive.org": "archive_org", "pixeldrain.com": "pixeldrain" };

  /** types.ts fmtBytes: the view model counts in MB. */
  const fmt = (mb) => (mb >= 1000 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(2)} MB`);
  const fmtShort = (mb) => (mb >= 1000 ? `${(mb / 1024).toFixed(1)} GB` : mb >= 10 ? `${Math.round(mb)} MB` : `${mb.toFixed(1)} MB`);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  function kindTile(kind, size = 22) {
    const k = KINDS[kind] || KINDS.other;
    return `<span class="kind-tile" style="width:${size}px;height:${size}px;color:${k.color};background:color-mix(in srgb, ${k.color} 16%, transparent)">${icon(k.icon, Math.round(size * 0.62))}</span>`;
  }

  function siteIcon(host, size = 14) {
    const file = BUNDLED[host];
    if (file) return `<img class="site-icon" src="${SRC}figma/assets/providers/${file}.png" width="${size}" height="${size}" alt="">`;
    return `<span class="site-letter" style="width:${size}px;height:${size}px;font-size:${Math.max(8, size * 0.56)}px">${esc(host[0].toUpperCase())}</span>`;
  }

  const siteTile = (host, size = 22) => `<span class="site-tile" style="width:${size}px;height:${size}px">${siteIcon(host, 14)}</span>`;

  const BADGE = {
    downloading: ["var(--accent-light)", "rgba(0,120,212,0.18)", "Downloading", true],
    resolving: ["#38bdf8", "rgba(56,189,248,0.16)", "Getting metadata", true],
    waiting: ["#fbbf24", "rgba(251,191,36,0.18)", "Waiting timer…", true],
    captcha: ["#a78bfa", "rgba(167,139,250,0.18)", "Solving captcha", true],
    paused: ["var(--warning)", "rgba(245,158,11,0.16)", "Paused", false],
    queued: ["var(--ink-45)", "var(--surface-08)", "Queued", false],
    completed: ["var(--success)", "rgba(20,174,92,0.14)", "Completed", false],
    failed: ["var(--danger)", "rgba(239,68,68,0.16)", "Failed", false],
  };
  function statusBadge(status, label) {
    const [c, bg, text, dot] = BADGE[status] || BADGE.queued;
    return `<span class="mk-badge" style="color:${c};background:${bg};border-color:color-mix(in srgb, ${c} 14%, transparent)">${dot ? `<span class="mk-bdot pulse-dot" style="background:${c}"></span>` : ""}<span>${esc(label || text)}</span></span>`;
  }

  const TONE = { downloading: "active", paused: "paused", completed: "done", failed: "error" };
  function progressCell(p, status) {
    const pc = Math.max(0, Math.min(100, Math.round(p)));
    return `<span class="dl-progress-cell"><span class="dl-progress-bar ${TONE[status] || ""}"><span style="width:${p}%"></span></span><span class="dl-progress-pct">${pc > 0 ? pc + "%" : "—"}</span></span>`;
  }

  function sizeCell(done, total, status) {
    if (status !== "completed" && done > 0) {
      const d = fmt(done), t = fmt(total);
      const same = d.split(" ")[1] === t.split(" ")[1];
      return `<span class="dl-size-cell"><span class="dl-size-done">${same ? d.split(" ")[0] : d}</span> / ${t}</span>`;
    }
    return `<span class="dl-size-cell">${fmt(total)}</span>`;
  }

  const hostCell = (host) => `<span class="dl-host-cell">${siteIcon(host)}<span class="mk-trunc">${esc(host)}</span></span>`;

  /** A speed trace like ui/Sparkline: deterministic wobble around the current rate. */
  function sparkline(speed, seed = 1, t = 0, w = 52, h = 16) {
    const n = 18, pts = [];
    for (let i = 0; i < n; i++) {
      const x = i + t * 2;
      const wob = 0.78 + 0.16 * Math.sin(x * 0.9 + seed) + 0.08 * Math.sin(x * 2.3 + seed * 3);
      pts.push([(i / (n - 1)) * w, h - 2 - wob * (h - 5)]);
    }
    const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join("");
    return `<span class="mk-spark"><svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><path d="${line}L${w} ${h}L0 ${h}Z" fill="color-mix(in srgb, var(--success) 14%, transparent)"/><path d="${line}" fill="none" stroke="var(--success)" stroke-width="1.2"/></svg><b>${speed.toFixed(1)} MB/s</b></span>`;
  }

  const eta = (mbLeft, speed) => {
    if (!speed) return "—";
    const s = Math.round(mbLeft / speed);
    return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
  };

  const dot = (tone) => `<span class="ui-dot ${tone}"></span>`;
  const cols = (list) => `--dt-cols:${list.join(" ")}`;
  const cell = (id, html, align) => `<div class="download-cell col-${id}"${align ? ` style="justify-content:${align}"` : ""}>${html}</div>`;
  const header = (defs) => `<div class="dl-col-header">${defs.map(([id, title, align]) =>
    `<div class="dl-col-head" data-header-cell="${id}"><span class="dl-col-head-label" style="justify-content:${align || "flex-start"}">${title}</span></div>`).join("")}</div>`;

  /** Pick columns from a table's defs: ids, or [id, width] to resize one for a tight crop. */
  const pick = (all, columns) => {
    if (!columns) return all;
    const want = new Map(columns.map((c) => (Array.isArray(c) ? c : [c, null])));
    return all.filter(([id]) => want.has(id)).map((d) => (want.get(d[0]) ? [d[0], d[1], d[2], want.get(d[0])] : d));
  };

  window.K = { pick, SRC, fmt, fmtShort, esc, kindTile, siteIcon, siteTile, statusBadge, progressCell, sizeCell, hostCell, sparkline, eta, dot, cols, cell, header };
})();
