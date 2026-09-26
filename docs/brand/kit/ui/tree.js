// The Explore tree for the sample page, in any state of a session: rows can be
// revealed one by one, Mirror 1 can be mid-follow, and the found file can be
// queued. Used by the feature cards and the demo timeline.
(() => {
  const X = KIT_DATA.explore;

  /**
   * o.reveal   number of rows shown (default all)
   * o.follow   "busy" while following Mirror 1, "done" once the file is found
   * o.steps    how many follow steps are shown (0..3)
   * o.count    seconds left on the countdown step while following
   * o.queued   the followed file is in Downloads
   * o.fresh    ids to mark as just arrived; o.enter {id: 0..1} for fade-ins
   * o.checks   ids that are ticked
   */
  K.sintelRows = (o = {}) => {
    const steps = o.steps ?? (o.follow === "done" ? 3 : 0);
    const following = o.follow === "busy";
    const done = o.follow === "done";
    const rows = [
      { id: "site", type: "site", label: X.site, host: X.site, open: true, kids: true, chip: "2 pages · 7 files", via: "Explored" },
      { id: "page", type: "source", depth: 1, label: X.page, host: X.site, open: true, kids: true, chip: "5 files", sub: "Sintel (2010) — open movie" },
      { id: "g1", type: "group", depth: 2, label: "Download buttons", open: true, kids: true, chip: "3" },
      { id: "m1", type: "button", depth: 3, label: X.buttons[0].label, sub: following ? "" : X.buttons[0].sub, host: X.buttons[0].host, follow: true,
        kids: steps > 0 || following, open: steps > 0, busy: following ? "Following…" : null, tag: done ? { tone: "ok", text: "3 steps" } : null,
        hint: o.hintFollow ? "Double-click to follow" : null },
      ...X.steps.slice(0, steps).map((s, i) => ({ id: `s${i}`, type: "step", depth: 4, label: s.label,
        sub: i === 1 && following && o.count != null ? `${o.count} s left · 3 ads blocked` : s.sub,
        busy: following && i === steps - 1 && i !== 1 ? "Clicking…" : null })),
      ...(done ? [{ id: "ff", type: "file", depth: 4, label: X.followed.name, ext: X.followed.ext, kind: X.followed.kind, size: X.followed.size,
        host: X.followed.host, match: 97, queued: o.queued, best: true }] : []),
      { id: "m2", type: "link", depth: 3, label: X.buttons[1].label, sub: X.buttons[1].sub, host: X.buttons[1].host, tag: { tone: "info", text: "shortlink" } },
      { id: "m3", type: "link", depth: 3, label: X.buttons[2].label, sub: X.buttons[2].sub, host: X.buttons[2].host },
      { id: "g2", type: "group", depth: 2, label: "Files", open: true, kids: true, chip: "5" },
      ...X.files.map((f) => ({ ...f, id: f.id, type: "file", depth: 3, label: f.name, best: f.best && !done })),
      { id: "extras", type: "source", depth: 1, label: "/films/sintel/extras", host: X.site, kids: true, chip: "2 files" },
    ];
    const shown = rows.slice(0, o.reveal ?? rows.length);
    return shown.map((r) => ({
      ...r,
      fresh: (o.fresh || []).includes(r.id),
      check: (o.checks || []).includes(r.id) ? "on" : "off",
      enter: o.enter && o.enter[r.id] != null ? o.enter[r.id] : undefined,
    }));
  };

  /** Filter counts for the sample page, as ExploreFilters shows them. */
  K.sintelCounts = { all: 7, kinds: [["video", 2], ["audio", 1], ["pictures", 1], ["subtitles", 1], ["archives", 2]], hosts: [["opencinema.org", 5], ["archive.org", 2]] };

  K.sintelStatus = (selected) => selected
    ? `<span class="xp-status-text"><b>${selected.n}</b> selected · ${K.fmt(selected.mb)}</span><span class="xp-bar-btn">Clear</span><span class="grow"></span><span class="btn-accent xp-bar-primary">${icon("download", 12)}Add ${selected.n} file${selected.n === 1 ? "" : "s"} to Downloads</span>`
    : `<span class="xp-status-text">1 site · 7 files</span><span class="grow"></span><span class="xp-legend"><kbd>Ctrl A</kbd>select all <kbd>Ctrl D</kbd>deselect <kbd>Enter</kbd>download <kbd>Shift F10</kbd>menu</span>`;
})();
