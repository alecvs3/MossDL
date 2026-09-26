// Renders the timeline at time t into the stage, and drives it either from the
// recorder (?capture=1: window.__seek(t) per frame) or in real time.
(() => {
  const stage = document.getElementById("stage");
  const cam = document.getElementById("cam");
  const app = document.getElementById("app");
  const fx = document.getElementById("fx");
  const titles = document.getElementById("titles");
  const caption = document.getElementById("caption");
  const cursor = document.getElementById("cursor");
  // ?theme=light plays the same session in the light app theme on a light stage.
  const light = params.get("theme") === "light";
  window.DEMO_THEME = light ? "daylight" : "midnight";
  stage.classList.toggle("light", light);
  cam.classList.toggle("light", light);
  const ARROW = '<svg class="cursor" viewBox="0 0 22 30"><path d="M2 2v22.5l6.2-5.9 4 9.2 3.9-1.7-4-9h8.6z" fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg>';

  function render(t) {
    const a = TL.app(t);
    E.morph(app, K.shell({ theme: DEMO_THEME, page: a.page, body: a.body, hoverNav: a.hoverNav }));
    const c = TL.cam(t);
    cam.style.transform = `translate(${c.x}px, ${c.y}px) scale(${c.s})`;
    cam.style.opacity = c.opacity;
    cam.style.filter = c.blur || c.dim ? `blur(${c.blur.toFixed(2)}px) brightness(${(1 - c.dim).toFixed(3)})` : "none";
    E.morph(fx, TL.fx(t));
    E.morph(titles, TITLES.titles(t));
    E.morph(caption, TITLES.captions(t));
    const p = E.cursorAt(TL.CURSOR, t, stage);
    const o = TL.cursorOpacity(t);
    E.morph(cursor, `${p.ring != null ? `<div class="ring" style="left:${p.x}px;top:${p.y}px;opacity:${((1 - p.ring) * o).toFixed(3)};transform:scale(${0.4 + p.ring * 0.9})"></div>` : ""}
      <div style="position:absolute;left:${p.x.toFixed(1)}px;top:${p.y.toFixed(1)}px;opacity:${o.toFixed(3)};transform:scale(${p.down ? 0.88 : 1});transform-origin:0 0">${ARROW}</div>`);
    E.syncAnimations(t);
  }

  window.__duration = TL.duration;
  window.__seek = (t) => render(t);

  // The static backdrop.
  document.getElementById("backdrop").innerHTML = topo(1600, 900, [{ cx: 1420, cy: 80, count: 22, r0: 30, gap: 32, seed: 2 }, { cx: 120, cy: 980, count: 14, r0: 60, gap: 36, seed: 5, tilt: 0.4 }], light ? { color: "rgba(47,110,60,1)", opacity: 0.1 } : {});

  if (params.get("capture")) {
    render(0);
    markReady();
  } else {
    // Real-time playback: loops; click to pause, arrow keys to step.
    let t = Number(params.get("t") || 0), last = performance.now(), paused = false;
    const tick = (now) => {
      if (!paused) t = (t + (now - last) / 1000) % TL.duration;
      last = now;
      render(t);
      requestAnimationFrame(tick);
    };
    stage.addEventListener("click", () => { paused = !paused; });
    addEventListener("keydown", (e) => { if (e.key === "ArrowRight") t += 0.5; if (e.key === "ArrowLeft") t = Math.max(0, t - 0.5); });
    requestAnimationFrame(tick);
    markReady();
  }
})();
