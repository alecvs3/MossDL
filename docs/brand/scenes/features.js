// Copy and visuals for each feature card (scenes/feature.html?f=<id>).
// Every claim here is a shipped capability; no speeds or statistics.
window.FEATURES = (() => {
  const win = (theme, page, body, o = {}) => K.frame({ x: o.x ?? 540, y: o.y ?? 64, w: o.w ?? 720, h: o.h ?? 680, zoom: o.zoom ?? 1.1,
    inner: K.shell({ theme, page, body, showToolbar: o.toolbar ?? false }) });

  return {
    downloads: {
      eyebrow: "Downloads", icon: "download",
      title: "Split into parts,<br><em>fetched in parallel.</em>",
      lede: "A long-lived Rust core moves every byte, with segmented transfers, retries and checksums.",
      facts: [
        ["layers", "File-host plugins", "Links from supported hosts resolve to the real file."],
        ["refreshCw", "Nothing lost on relaunch", "Unfinished downloads come back paused, progress intact."],
      ],
      visual: (t) => win(t, "downloads", K.downloadsPage({ list: S.downloads(), t: 3, columns: ["select", ["name", "minmax(120px,1fr)"], "status", ["speed", "108px"], ["pct", "100px"]] }), { x: 520, w: 660, zoom: 1 })
        + K.card({ x: 482, y: 200, w: 650, zoom: 1.3, theme: t, inner: `<div class="card-head">${icon("zap", 12)} Transferring <span class="sub">· 2 active</span><span class="grow"></span><span class="mk-total">↓ 50.5 MB/s</span></div>`
          + K.downloadsPage({ list: S.downloads().filter((d) => d.status === "downloading"), t: 5, bare: true, grouped: false, columns: ["select", ["name", "minmax(100px,1fr)"], ["speed", "112px"], ["pct", "100px"]] }) }),
    },
    explore: {
      eyebrow: "Explore", icon: "globe",
      title: "A whole page,<br><em>as a tree.</em>",
      lede: "Paste a page, a folder or a shortlink. Explore lists every file, download button and mirror on it, and stars the best match.",
      facts: [
        ["star", "Best match first", "Each file is scored by how likely it is the one you want."],
        ["sliders", "Filter by type, host and size", "Then add one file or a whole selection to Downloads."],
      ],
      visual: (t) => win(t, "explore", K.explorePage({ url: KIT_DATA.explore.url, rows: K.sintelRows({ checks: ["f1", "f2"] }), columns: ["select", "name", "size", "match"],
        status: K.sintelStatus({ n: 2, mb: 1835 }) }), { x: 500, w: 660, zoom: 1.02, y: 64, h: 612 })
        + K.filtersCard({ x: 458, y: 446, w: 206, h: 228, zoom: 0.95, theme: t, sections: ["kind"] }),
    },
    following: {
      eyebrow: "Follow", icon: "cursor",
      title: "Past the buttons,<br><em>countdowns and ads.</em>",
      lede: "Follow a download button and MossDL clicks through mirror pages and countdowns in its own browser, on your route, until the file appears.",
      facts: [
        ["ban", "Ads blocked", "uBlock Origin's filter lists, compiled into the core."],
        ["step", "Every click shown as a step", "And it stops and says so when a captcha appears."],
      ],
      visual: (t) => win(t, "explore", K.explorePage({ url: KIT_DATA.explore.url, rows: K.sintelRows({ follow: "busy", steps: 2, count: 7, reveal: 9 }), columns: ["select", "name"],
        status: K.sintelStatus() }), { x: 520, w: 640, zoom: 1.06, y: 64, h: 612 })
        + K.browserCard({ x: 790, y: 436, w: 380, theme: t, count: 7, compact: true }),
    },
    connections: {
      eyebrow: "Connections", icon: "routes",
      title: "Choose the road<br><em>downloads take.</em>",
      lede: "Send downloads through a VPN location or a proxy. WireGuard runs inside MossDL, with no driver, adapter or admin rights.",
      facts: [
        ["key", "Sign in to Mullvad", "With your account number; every location is listed for you."],
        ["swap", "Auto-switch on quota", "Hit a host's limit and the download moves to the next healthy route."],
      ],
      visual: (t) => win(t, "routes", K.connectionsPage({ active: "sto", picked: "sto", noList: true, headSwitch: false, columns: ["status", "name", "latency"] }), { x: 520, w: 660, zoom: 0.98, y: 64, h: 612 }),
    },
    captchas: {
      eyebrow: "Captchas", icon: "captchas",
      title: "Solved for you,<br><em>or handed to you.</em>",
      lede: "The solvers try first. When one needs a person, open it in your own browser: the MossDL extension brings the answer back.",
      facts: [
        ["shield", "Verified handoff", "A single-use ticket ties each answer to its download, site and browser."],
        ["history", "Resumes on its own", "The download continues once the site accepts the answer."],
      ],
      visual: (t) => win(t, "captchas", K.captchasPage(), { x: 520, w: 660, zoom: 0.94, y: 64, h: 440 })
        + K.handoffCard({ x: 740, y: 350, w: 420, zoom: 1.1, theme: t }),
    },
  };
})();
