// Explore page: URL bar, the tree (pages/explore/ExploreRow.tsx), the filter
// column (ExploreFilters.tsx) and the selection/status bar.
(() => {
  const { esc, fmt, kindTile, siteIcon, siteTile, cell, header, cols } = K;
  const INDENT = 18;
  const TYPE_ICON = { button: "cursor", step: "step", folder: "folder", link: "link" };
  const DEFS = [
    ["select", "", "center", "30px"], ["name", "Name", null, "minmax(200px,1fr)"], ["kind", "Type", null, "52px"],
    ["size", "Size", "flex-end", "76px"], ["host", "Host", null, "138px"], ["match", "Match", null, "96px"],
    ["added", "Added", "flex-end", "70px"], ["actions", "", "flex-end", "56px"],
  ];

  /** r: { id, type, depth, label, sub, host, kind, ext, size, match, best, low, fresh, open, kids, busy, tag, via, chip, queued, check, hint, enter } */
  function row(r, o) {
    const header = r.type === "site" || r.type === "source";
    const ico = header ? siteTile(r.host) : r.type === "group" ? "" : r.type === "file" ? kindTile(r.kind, 18)
      : `<span class="xp-ico">${icon(TYPE_ICON[r.type] || "link", 14)}</span>`;
    const name = `<div class="xp-name" style="padding-left:${(r.depth || 0) * INDENT}px">
      <span class="xp-chev${r.kids ? "" : " none"}${r.open ? " open" : ""}">${icon("chevronRight", 10)}</span>${ico}
      <span class="xp-label">${esc(r.label)}</span>
      ${r.fresh === true || r.isNew ? '<span class="xp-new">New</span>' : ""}${r.best ? '<span class="xp-star">★</span>' : ""}
      ${r.chip ? `<span class="xp-count-chip">${esc(r.chip)}</span>` : ""}
      ${r.via ? `<span class="xp-tag">${r.via}</span>` : ""}
      ${r.queued ? `<span class="xp-tag ok">${icon("check", 9)} In Downloads</span>` : ""}
      ${r.tag ? `<span class="xp-tag ${r.tag.tone}">${esc(r.tag.text)}</span>` : ""}
      ${r.sub ? `<span class="xp-sub">${esc(r.sub)}</span>` : ""}
      ${r.busy ? `<span class="xp-spin"></span><span class="xp-sub">${esc(r.busy)}</span>` : ""}
      ${r.hint ? `<span class="xp-hint mk-show">${esc(r.hint)}</span>` : ""}
    </div>`;
    const cells = {
      select: `<span class="xp-check ${r.check || "off"}"></span>`,
      name,
      kind: r.ext ? `<span class="xp-kind">${esc(r.ext)}</span>` : "",
      size: `<span class="xp-num">${r.size ? fmt(r.size) : r.sizeText || ""}</span>`,
      host: r.host && r.type !== "group" ? `<span class="xp-host">${siteIcon(r.host)}<span class="mk-trunc">${esc(r.host)}</span></span>` : "",
      match: r.match ? `<span class="xp-match${r.match >= 85 ? " hi" : ""}"><span class="xp-bar"><span style="width:${r.match}%"></span></span><b>${r.match}%</b></span>` : "",
      added: r.type === "group" ? "" : `<span class="xp-when">${r.added || "just now"}</span>`,
      actions: r.type === "group" ? "" : `<span class="xp-acts"><button class="xp-act-primary${o.hover === r.id ? " mk-show" : ""}">${icon(r.follow ? "play" : "download", 12)}</button><button class="xp-act-menu">${icon("dots", 12)}</button></span>`,
    };
    const cls = `dt-row xp-row xp-${r.type}${r.check === "on" ? " selected" : ""}${o.focus === r.id ? " focused" : ""}${r.low ? " low" : ""}${r.best ? " best" : ""}${r.fresh ? " mk-fresh" : ""}${r.open ? " open" : ""}${r.depth ? " nested" : ""}${o.hover === r.id ? " mk-hover" : ""}`;
    const fade = typeof r.fresh === "number" ? `--fresh:${r.fresh.toFixed(3)};` : "";
    const style = r.enter != null || fade ? ` style="${fade}${r.enter != null ? `opacity:${r.enter};transform:translateY(${(1 - r.enter) * -5}px)` : ""}"` : "";
    return `<div data-k="${r.id}" class="${cls}"${style}>${o.defs.map(([id, , align]) => cell(id, cells[id], align)).join("")}</div>`;
  }

  function filters(counts, sections = ["kind", "host", "size"]) {
    const fi = (inner, n, on) => `<div class="xp-fi${on ? " on" : ""}">${inner}<span class="xp-count">${n}</span></div>`;
    return `<aside class="xp-filters">
      <div class="xp-filters-head"><span>${icon("sliders", 12)} Filters</span><span class="xp-fh-clear" style="color:var(--ink-25)">Reset</span></div>
      <label class="xp-filter-q">${icon("search", 12)}<span class="mk-ph">Filter names</span><kbd>Ctrl F</kbd></label>
      ${sections.includes("kind") ? `<div class="xp-fsec"><div class="xp-fh">File type</div>
        ${fi(`<span class="xp-all-tile">${icon("layers", 11)}</span><span class="mk-trunc">All files</span>`, counts.all, true)}
        ${counts.kinds.map(([k, n]) => fi(`${kindTile(k, 16)}<span class="mk-trunc">${KINDS[k].label}</span>`, n)).join("")}
      </div>` : ""}
      ${sections.includes("host") ? `<div class="xp-fsec"><div class="xp-fh">Host</div>
        ${counts.hosts.map(([h, n]) => fi(`<span class="xp-mini-check"></span>${siteIcon(h, 14)}<span class="mk-trunc">${h}</span>`, n)).join("")}
      </div>` : ""}
      ${sections.includes("size") ? `<div class="xp-fsec"><div class="xp-fh">Size</div>
        ${["Any size", "Under 10 MB", "10 MB – 1 GB", "Over 1 GB"].map((l, i) => `<div class="xp-fi${i ? "" : " on"}"><span class="xp-radio${i ? "" : " on"}"></span><span class="mk-trunc">${l}</span></div>`).join("")}
      </div>` : ""}
    </aside>`;
  }

  /** opts: url, caret, rows, counts (filters shown when given), status, focus, hover, press ("go") */
  K.explorePage = ({ url = "", caret = false, rows = [], counts, status, focus, hover, press, useBrowser = false, empty, columns }) => {
    const defs = K.pick(DEFS, columns);
    const o = { defs, focus, hover };
    const input = url ? `<span class="mk-url">${esc(url)}</span>` : `<span class="mk-ph">Paste a page, folder, shortlink or several links</span>`;
    const table = rows.length ? `<div class="dt-scroll xp-table" style="${cols(defs.map((d) => d[3]))}"><div class="dt-content">${header(defs)}${rows.map((r) => row(r, o)).join("")}</div></div>`
      : `<div class="xp-empty">${icon("globe", 26)}<p>${empty || "Paste a page to see every file, button and mirror on it."}</p></div>`;
    return `<div class="xp-page">
      <div class="xp-head">
        <label class="xp-url${caret ? " mk-focus" : ""}">${icon("globe", 14)}${input}${caret ? '<span class="mk-caret"></span>' : ""}<span class="grow"></span><kbd>Ctrl L</kbd></label>
        <label class="xp-browser"><span class="mk-cb${useBrowser ? " on" : ""}"></span> Use browser</label>
        <button class="btn-accent xp-go${press === "go" ? " mk-press" : ""}">Explore</button>
        <button class="xp-icon-btn${counts ? " on" : ""}">${icon("sliders", 14)}</button>
      </div>
      <div class="xp-body${counts ? "" : " no-filters"}">${table}${counts ? filters(counts) : ""}</div>
      <div class="xp-status">${status || '<span class="xp-status-text">Nothing explored yet</span>'}</div>
    </div>`;
  };
  K.exploreFilters = filters;
  K.exploreRow = (r, o = {}) => row(r, { defs: o.defs || DEFS, ...o });
  K.EXPLORE_DEFS = DEFS;
})();
