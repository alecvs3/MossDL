// The demo's story. Edit times in `T`, words in CAPTIONS/TITLES, and what the
// app shows in `app(t)`; the player renders whatever these return for time t.
window.TL = (() => {
  const { seg, ease, clamp, lerp } = E;
  const X = KIT_DATA.explore;

  // Beat boundaries (seconds). Everything else is relative to these.
  const T = {
    introOut: 2.1, winIn: 2.2, urlClick: 3.3, typeFrom: 3.45, typeTo: 4.55, go: 4.95,
    crawl: 5.1, reveal: 5.55, interA: 8.15, follow: 9.95, card: 10.3, count: 10.75, found: 12.55,
    add: 13.2, toDownloads: 13.8, interB: 17.3, toRoutes: 17.9, pick: 19.2, connect: 19.8, up: 20.45,
    toHistory: 21.2, outro: 22.9, end: 25.2,
  };
  const CAPTIONS = [
    [2.9, 5.5, "01", "Paste a page"],
    [5.6, 8.1, "02", "Explore maps every file, button and mirror"],
    [9.4, 13.5, "03", "It follows the button in its own browser, ads blocked"],
    [14.0, 17.25, "04", "Segmented downloads on a Rust core"],
    [18.5, 21.1, "05", "WireGuard tunnels run inside MossDL"],
    [21.4, 22.85, "06", "Everything finished, kept in History"],
  ];
  const INTERSTITIALS = [
    [T.interA, T.interA + 1.15, "Mirror pages. Countdowns. Ads.", "Followed for you."],
    [T.interB, T.interB + 1.15, "Then pick the road", "your downloads take."],
  ];

  // --- the app at time t ---
  // Follow steps appear as the browser gets to them: the button, the countdown, the link.
  const STEP_AT = [T.follow + 0.4, T.count, T.count + 1.5];
  const reveal = (t) => (t < T.crawl ? 0 : t < T.reveal ? 1 : 1 + Math.floor((t - T.reveal) / 0.13));
  function exploreState(t) {
    const count = Math.max(0, Math.ceil(5 - (t - T.count) / 0.3));
    const following = t >= T.follow + 0.1 && t < T.found;
    const steps = following ? STEP_AT.filter((s) => t >= s).length : t >= T.found ? 3 : 0;
    const rows = K.sintelRows({
      follow: t >= T.found ? "done" : following ? "busy" : null, steps, count, queued: t >= T.add + 0.1,
      reveal: reveal(t), hintFollow: t > T.follow - 0.25 && t < T.follow + 0.1,
    }).map((r, i) => {
      const born = r.id === "site" ? T.crawl : r.id === "ff" ? T.found : r.type === "step" ? STEP_AT[Number(r.id[1])] : T.reveal + (i - 1) * 0.13;
      const age = t - born;
      return { ...r, isNew: r.id === "ff", enter: clamp(age / 0.25), fresh: age < 1.6 ? 1 - ease.in(clamp(age / 1.6)) : undefined };
    });
    if (t < T.reveal) rows[0] && (rows[0].busy = "Exploring…");
    const typed = X.url.slice(0, Math.round(X.url.length * clamp((t - T.typeFrom) / (T.typeTo - T.typeFrom))));
    return K.explorePage({
      url: t < T.typeFrom ? "" : typed, caret: t > T.urlClick && t < T.go, rows, press: t > T.go && t < T.go + 0.15 ? "go" : null,
      hover: t > T.follow - 0.25 && t < T.follow + 0.2 ? "m1" : t > T.add - 0.3 && t < T.add + 0.2 ? "ff" : null,
      empty: t < T.go ? undefined : "Exploring opencinema.org…",
      status: t < T.crawl ? undefined : t < T.reveal + 1.6 ? '<span class="xp-status-text">Exploring…</span>' : K.sintelStatus(),
    });
  }

  const SINTEL = { id: "sintel", name: X.followed.name, host: X.followed.host, kind: "video", size: X.followed.size, added: "just now" };
  function downloadsState(t) {
    const dt = t - T.toDownloads;
    const wob = (seed) => 1 + 0.05 * Math.sin(t * 2.1 + seed) + 0.03 * Math.sin(t * 5.3 + seed * 2);
    const base = S0.map((d, i) => d.status !== "downloading" ? d : { ...d, speed: d.speed * wob(i), done: Math.min(d.size, d.done + d.speed * dt) });
    const resolving = dt < 0.75;
    const ramp = ease.out(clamp((dt - 0.75) / 0.8));
    const sin = { ...SINTEL, status: resolving ? "resolving" : "downloading", badge: resolving ? "resolving" : null, speed: 34.2 * ramp * wob(9),
      done: resolving ? 0 : 34.2 * Math.max(0, dt - 1.1), seed: 4, enter: clamp(dt / 0.3), flash: dt < 1.4 };
    return K.downloadsPage({ list: [sin, ...base], t, columns: ["select", "name", "status", "host", "speed", "eta", "pct", "size"] });
  }
  const S0 = KIT_DATA.downloads.map((d, i) => ({ ...d, seed: i * 1.7 + 1 }));

  function app(t) {
    if (t < T.toDownloads + 0.05) return { page: "explore", body: exploreState(t), hoverNav: t > T.toDownloads - 0.3 ? "downloads" : null };
    if (t < T.toRoutes) return { page: "downloads", body: downloadsState(t) };
    if (t < T.toHistory + 0.05) {
      return { page: "routes", hoverNav: t > T.toHistory - 0.3 ? "history" : null,
        body: K.connectionsPage({ active: t >= T.connect + 0.05 ? "sto" : null, checking: t >= T.connect + 0.05 && t < T.up, picked: t >= T.pick ? "sto" : "ams",
          press: t > T.connect && t < T.connect + 0.15 ? "connect" : null, hover: t > T.pick - 0.3 && t < T.pick ? "sto" : null }) };
    }
    return { page: "history", body: K.historyPage({ items: KIT_DATA.history, fresh: clamp((t - T.toHistory - 0.1) / 0.35) }) };
  }

  // --- camera: the window rises in, pushes in gently, and recedes at the end ---
  const CAM = [[T.winIn, 1, 0, 60], [T.winIn + 0.8, 1, 0, 0], [T.reveal, 1, 0, 0], [T.reveal + 2, 1.035, 10, 14], [T.follow - 1, 1.035, 10, 14],
    [T.found, 1.05, -10, 24], [T.toDownloads, 1, 0, 0], [T.interB, 1.03, 0, 10], [T.toRoutes + 0.4, 1, 0, 0], [T.outro, 1, 0, 0], [T.outro + 0.8, 0.9, 0, -30]];
  function cam(t) {
    let i = CAM.findIndex((k) => k[0] > t);
    if (i === -1) i = CAM.length;
    const a = CAM[Math.max(0, i - 1)], b = CAM[Math.min(CAM.length - 1, i)];
    const p = a === b ? 1 : ease.inOut(clamp((t - a[0]) / (b[0] - a[0])));
    const inter = INTERSTITIALS.map(([a0, a1]) => Math.min(seg(t, a0 - 0.1, a0 + 0.25), 1 - seg(t, a1 - 0.3, a1 + 0.05))).reduce((m, v) => Math.max(m, v), 0);
    const opacity = t < T.winIn ? 0 : Math.min(seg(t, T.winIn, T.winIn + 0.6, ease.out), 1 - seg(t, T.outro + 0.2, T.outro + 0.8));
    return { s: lerp(a[1], b[1], p), x: lerp(a[2], b[2], p), y: lerp(a[3], b[3], p), blur: inter * 7 + seg(t, T.outro, T.outro + 0.8) * 8, dim: inter * 0.55, opacity };
  }

  // --- floating browser while following Mirror 1 ---
  function fx(t) {
    if (t < T.card || t > T.found + 0.7) return "";
    const p = seg(t, T.card, T.card + 0.4, ease.out) * (1 - seg(t, T.found + 0.2, T.found + 0.6, ease.in));
    const count = Math.max(0, Math.ceil(5 - (t - T.count) / 0.3));
    return `<div style="position:absolute;inset:0;opacity:${p.toFixed(3)};transform:translateX(${((1 - p) * 60).toFixed(1)}px)">${K.browserCard({ theme: DEMO_THEME, x: 1086, y: 318, w: 440, zoom: 1.12, compact: true, count: t < T.count ? 5 : count, press: count === 0 && t > T.found - 0.2 })}</div>`;
  }

  // --- cursor path (arrival times) ---
  const CURSOR = [
    { t: 0, at: [900, 980] }, { t: T.urlClick, at: ".xp-url .mk-ph", click: true, move: 0.8 }, { t: T.go, at: ".xp-go", click: true, move: 0.45 },
    { t: T.follow - 0.25, at: '[data-k="m1"] .xp-label', move: 0.7 }, { t: T.follow - 0.12, at: '[data-k="m1"] .xp-label', click: true },
    { t: T.follow, at: '[data-k="m1"] .xp-label', click: true }, { t: T.follow + 1.4, at: [700, 610], move: 1.0 },
    { t: T.add, at: '[data-k="ff"] .xp-act-primary', click: true, move: 0.55 }, { t: T.toDownloads, at: '[data-nav="downloads"]', click: true, move: 0.5 }, { t: T.toDownloads + 1.3, at: [1230, 610], move: 0.9 },
    { t: T.interB, at: [700, 640], move: 1.4 }, { t: T.toRoutes, at: '[data-nav="routes"]', click: true, move: 0.35 },
    { t: T.pick, at: '[data-k="sto"] .mk-trunc', click: true, move: 0.7 }, { t: T.connect, at: ".cx-side-primary", click: true, move: 0.5 },
    { t: T.toHistory, at: '[data-nav="history"]', click: true, move: 0.7 }, { t: T.outro + 0.4, at: [820, 1000], move: 0.8 },
  ];
  const cursorOpacity = (t) => Math.min(seg(t, 2.6, 3.0), 1 - seg(t, T.outro, T.outro + 0.4)) *
    (1 - INTERSTITIALS.map(([a0, a1]) => Math.min(seg(t, a0 - 0.1, a0 + 0.2), 1 - seg(t, a1 - 0.2, a1))).reduce((m, v) => Math.max(m, v), 0));

  return { T, CAPTIONS, INTERSTITIALS, app, cam, fx, CURSOR, cursorOpacity, duration: T.end };
})();
