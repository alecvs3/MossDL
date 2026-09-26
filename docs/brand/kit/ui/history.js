// History and Captchas pages (pages/HistoryPage.tsx, pages/CaptchasPage.tsx,
// captchas/CaptchaQueue.tsx).
(() => {
  const { esc, fmt, kindTile, statusBadge, hostCell, cell, header, cols } = K;
  const DEFS = [["expand", "", "center", "24px"], ["name", "File", null, "minmax(200px,1fr)"], ["activity", "Activity", null, "96px"], ["size", "Size", "flex-end", "76px"],
    ["took", "Took", "flex-end", "56px"], ["status", "Status", null, "96px"], ["host", "Host", null, "150px"], ["finished", "Finished", "flex-end", "84px"]];
  const mono = (s, c = "var(--ink-45)") => `<span class="mk-mono9" style="color:${c}">${s}</span>`;

  function activity(a) {
    if (!a) return mono("—", "var(--ink-25)");
    const ic = a.includes("captcha") ? "captchas" : "step";
    return `<span class="ui-chip mk-actchip">${icon(ic, 10)} ${esc(a)}</span>`;
  }

  K.historyPage = ({ items = KIT_DATA.history, fresh } = {}) => {
    const rows = items.map((h, i) => {
      const cells = {
        expand: `<span class="mk-expand">${icon("chevronRight", 13)}</span>`,
        name: `<div class="mk-hname">${kindTile(h.kind, 20)}<div class="mk-namecell"><p class="mk-name" style="font-weight:500">${esc(h.name)}</p><p class="mk-sub">C:\\Users\\you\\Downloads</p></div></div>`,
        activity: activity(h.activity),
        size: mono(fmt(h.size)),
        took: mono(h.took, "var(--ink-40)"),
        status: statusBadge("completed"),
        host: hostCell(h.host),
        finished: mono(h.finished, "var(--ink-35)"),
      };
      const enter = i === 0 && fresh != null ? ` style="opacity:${fresh};transform:translateY(${(1 - fresh) * -6}px)"` : "";
      return `<div data-k="h${i}-${h.name}" class="dl-row-small${i === 0 && fresh != null ? " mk-flash" : ""}"${enter}>${DEFS.map(([id, , a]) => cell(id, cells[id], a)).join("")}</div>`;
    }).join("");
    const total = items.reduce((s, h) => s + h.size, 0);
    const chip = (label, n, on, c) => `<span class="mk-hfilter${on ? " on" : ""}" style="${on ? `border-color:${c}44` : ""}">${c && label !== "All" ? `<span class="mk-bdot" style="background:${c}"></span>` : ""}${label}<span class="mk-mono9" style="color:var(--ink-35)">${n}</span></span>`;
    return `<div class="mk-dlhead"><div class="mk-dlhead-l"><h1>History</h1><div class="mk-hfilters">${chip("All", items.length, true, "var(--ink-40)")}${chip("completed", items.length, false, "var(--success)")}${chip("failed", 0, false, "var(--danger)")}</div></div>
        <span class="mk-mono9" style="font-size:10px;color:var(--ink-35)">${items.length} finished · ${fmt(total)}</span><label class="win-input mk-filter" style="width:168px">${icon("search", 11)}<span>Search history…</span></label></div>
      <div class="dt-scroll mk-dltable" style="${cols(DEFS.map((d) => d[3]))}"><div class="dt-content">${header(DEFS)}<div class="mk-rows mk-rows-tight">${rows}</div></div></div>`;
  };

  const STATE = { manual_required: "Needs you", verifying: "Checking the answer with the site", solving: "Being solved" };
  function captchaItem(c) {
    const busy = c.state === "verifying";
    return `<article class="cp-item">
      <div class="cp-item-head"><span class="cp-vendor">${icon("shield", 16)}</span>
        <div class="grow"><div class="cp-item-title">${esc(c.host)} wants a captcha <span class="ui-chip">${esc(c.type)}</span></div>
          <div class="cp-item-sub">${STATE[c.state]}: ${esc(c.reason)}</div></div>
        <span class="cp-timer">Time left<b>${c.left}s</b></span></div>
      <div class="cp-item-actions">
        <span class="ui-input mk-input"><span class="mk-ph">Paste the token or the direct download link</span></span>
        <button class="btn-accent ui-primary"${busy ? "" : " disabled"}>${busy ? "Sending…" : "Submit"}</button>
        <button class="ui-btn">${icon("zap", 12)} Solve automatically</button>
        <button class="ui-btn${c.press ? " mk-press" : ""}">${icon("externalLink", 12)} Solve in browser</button>
        <button class="ui-btn ghost">Skip</button>
      </div></article>`;
  }

  K.captchasPage = ({ items } = {}) => {
    const list = items || [
      { host: "filebin.cc", type: "turnstile", state: "manual_required", reason: "the solver handed it to you", left: 214, press: true },
      { host: "courses.example.edu", type: "recaptcha_v2", state: "verifying", reason: "answer returned from your browser", left: 176 },
    ];
    const switches = `<span class="head-switch">Solve automatically <span class="mini-switch on"></span></span><span class="head-switch">Open in browser <span class="mini-switch"></span></span>`;
    return `<div class="page-shell">${K.pageHead([["waiting", "Waiting", list.length, true], ["history", "History"], ["shortlinks", "Shortlinks"], ["solvers", "Solvers"]], "waiting", switches)}
      <div class="cp-body">${list.map(captchaItem).join("")}</div></div>`;
  };
})();
