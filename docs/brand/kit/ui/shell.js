// The app window: TitleBar + MenuBar, GlobalToolbar, Sidebar and a page slot,
// as laid out by src/figma/FigmaApp.tsx. `theme` is the app's data-theme value
// ("midnight" is the default dark theme, the stylesheet's :root; "daylight" is the light one).
(() => {
  const { SRC } = K;

  const WINCTL = [
    '<path d="M0.5 5.5h10" />',
    '<rect x="0.5" y="0.5" width="10" height="10" rx="1.5" />',
    '<path d="M0.5 0.5l10 10M10.5 0.5l-10 10" />',
  ].map((p) => `<span><svg width="11" height="11" viewBox="0 0 11 11" fill="none" stroke="currentColor" stroke-width="1">${p}</svg></span>`).join("");

  function titleBar() {
    return `<div class="app-titlebar mk-titlebar">
      <div class="mk-tb-left">
        <img src="${SRC}figma/assets/brand/mossdl-logo.svg" width="16" height="16" alt="" style="border-radius:4px">
        <span class="mk-appname">MossDL</span>
        <div class="menu-bar">${["File", "View", "Tools", "Help"].map((m) => `<div class="menu-trigger">${m}</div>`).join("")}</div>
      </div>
      <div class="mk-winctl">${WINCTL}</div>
    </div>`;
  }

  const TOOLBAR = [
    [["Resume", "playAll", "resume"], ["Pause", "pauseAll", "pause"], ["Stop", "stopAll", "stop"]],
    [["Retry", "refreshCw", "retry"], ["Dedupe", "dedupe", "dedupe"], ["Clear", "clearDone", "clear"], ["Folder", "folder", "folder"]],
    [["Delete", "trash", "delete"], ["Copy", "clipboardList", "copy"], ["Add URL", "plus", "add-url"]],
  ];
  function toolbar(press) {
    return `<div class="dl-toolbar mk-toolbar">${TOOLBAR.map((group, gi) =>
      `${gi ? '<div class="dl-toolbar-sep"></div>' : ""}<div class="dl-toolbar-group">${group.map(([label, ic, variant]) =>
        `<button class="dl-toolbar-btn btn-${variant}${press === variant ? " mk-press" : ""}">${icon(ic, 16, "dl-toolbar-icon")}<span class="dl-toolbar-label">${label}</span></button>`).join("")}</div>`).join("")}</div>`;
  }

  const NAV = [
    ["downloads", "download", "Downloads"], ["explore", "globe", "Explore"], ["captchas", "captchas", "Captchas"],
    ["routes", "routes", "Connections"], ["history", "history", "History"], ["settings", "settings", "Settings"],
  ];
  function sidebar(page, badges = {}, hover) {
    return `<aside class="app-sidebar mk-sidebar"><div class="mk-nav">${NAV.map(([id, ic, label]) => {
      const n = badges[id];
      const badge = n ? `<span class="mk-navbadge ${id === "captchas" ? "amber" : "sky"}">${n}</span>` : "";
      const cls = `nav-item${page === id ? " active" : ""}${id === "captchas" ? " nav-item-captcha" : ""}${id === "captchas" && n ? " has-pending" : ""}${hover === id ? " mk-hover" : ""}`;
      return `<button class="${cls}" data-nav="${id}"><div class="mk-navicon">${icon(ic, 17)}${badge}</div><span class="mk-navlabel">${label}</span></button>`;
    }).join("")}</div></aside>`;
  }

  /** The whole window. Pass the page's HTML as `body`. */
  K.shell = ({ theme = "midnight", page = "downloads", body = "", showToolbar = true, badges, hoverNav, press } = {}) =>
    `<div class="figma-shell mk-shell"${theme === "midnight" ? "" : ` data-theme="${theme}"`}>
      <div class="mk-noise"></div>
      ${titleBar()}
      ${showToolbar ? toolbar(press) : ""}
      <div class="mk-body">${sidebar(page, badges, hoverNav)}<main class="mk-main">${body}</main></div>
    </div>`;

  /** A positioned, zoomed window frame on a stage. w/h are stage pixels. */
  K.frame = ({ x = 0, y = 0, w, h, zoom = 1, cls = "", style = "", inner }) =>
    `<div class="mk-frame ${cls}" style="left:${x}px;top:${y}px;width:${w}px;height:${h}px;${style}">
      <div class="mk-zoom" style="zoom:${zoom};width:${w / zoom}px;height:${h / zoom}px">${inner}</div>
    </div>`;

  /** A page header with tabs, as ui/PageTabs renders them. */
  K.pageHead = (tabs, on, right = "") =>
    `<div class="page-head"><div class="page-tabs">${tabs.map(([id, label, count, alert]) =>
      `<button class="page-tab${id === on ? " on" : ""}">${label}${count != null ? ` <span class="page-tab-count${alert ? " alert" : ""}">${count}</span>` : ""}</button>`).join("")}</div><span class="grow"></span>${right}</div>`;
})();
