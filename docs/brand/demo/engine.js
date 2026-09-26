// Demo engine: a pure function of time. `render(t)` asks the timeline for the
// scene at t and patches the DOM to match, so any frame can be seeked to
// directly (window.__seek for the recorder) or played in real time.
window.E = (() => {
  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
  const ease = {
    linear: (x) => x,
    out: (x) => 1 - Math.pow(1 - x, 3),
    in: (x) => x * x * x,
    inOut: (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2),
    back: (x) => 1 + 2.2 * Math.pow(x - 1, 3) + 1.2 * Math.pow(x - 1, 2),
  };
  /** Progress of t through [a, b], clamped and eased. */
  const seg = (t, a, b, e = ease.inOut) => e(clamp((t - a) / (b - a)));
  const lerp = (a, b, p) => a + (b - a) * p;

  // --- DOM morphing: keep nodes (and their images) alive between frames ---
  function patchAttrs(x, y) {
    for (const { name } of [...x.attributes]) if (!y.hasAttribute(name)) x.removeAttribute(name);
    for (const { name, value } of [...y.attributes]) if (x.getAttribute(name) !== value) x.setAttribute(name, value);
  }
  function patchChildren(a, b) {
    const keyed = new Map();
    for (const c of a.childNodes) if (c.nodeType === 1 && c.dataset.k) keyed.set(c.dataset.k, c);
    let cur = a.firstChild;
    for (const y of [...b.childNodes]) {
      const k = y.nodeType === 1 ? y.dataset.k : null;
      let x = k ? keyed.get(k) || null : cur;
      if (k && x) { keyed.delete(k); if (x !== cur) a.insertBefore(x, cur); }
      if (!k && x && x.nodeType === 1 && x.dataset.k) x = null;
      if (x && x.nodeType === y.nodeType && x.nodeName === y.nodeName) {
        if (x.nodeType === 1) { patchAttrs(x, y); patchChildren(x, y); }
        else if (x.nodeValue !== y.nodeValue) x.nodeValue = y.nodeValue;
        cur = x.nextSibling;
      } else {
        a.insertBefore(y, cur);
      }
    }
    while (cur) { const n = cur.nextSibling; a.removeChild(cur); cur = n; }
  }
  const tpl = document.createElement("template");
  const last = new WeakMap();
  function morph(el, html) {
    if (last.get(el) === html) return;
    last.set(el, html);
    tpl.innerHTML = html;
    patchChildren(el, tpl.content);
  }

  // --- cursor: glide between measured targets ---
  const seen = new Map();
  function pointOf(target, stage) {
    if (Array.isArray(target)) return target;
    const el = document.querySelector(target);
    if (el) {
      const r = el.getBoundingClientRect(), s = stage.getBoundingClientRect(), k = s.width / stage.offsetWidth;
      seen.set(target, [(r.left - s.left + r.width * 0.5) / k, (r.top - s.top + r.height * 0.55) / k]);
    }
    return seen.get(target) || [800, 950];
  }
  /** keys: [{ t: arrival time, at: selector | [x, y], move: seconds of travel, click }] sorted by t. */
  function cursorAt(keys, t, stage) {
    let i = keys.findIndex((k) => k.t > t);
    if (i === -1) i = keys.length;
    const a = keys[Math.max(0, i - 1)], b = keys[Math.min(keys.length - 1, i)];
    const pa = pointOf(a.at, stage), pb = pointOf(b.at, stage);
    const start = Math.max(a.t, b.t - (b.move ?? 0.6));
    const p = a === b ? 1 : ease.inOut(clamp((t - start) / Math.max(0.001, b.t - start)));
    const lift = Math.sin(p * Math.PI) * Math.min(40, Math.hypot(pb[0] - pa[0], pb[1] - pa[1]) * 0.08);
    const clickAt = keys.filter((k) => k.click && k.t <= t).map((k) => k.t).pop();
    const since = clickAt == null ? 9 : t - clickAt;
    return { x: lerp(pa[0], pb[0], p), y: lerp(pa[1], pb[1], p) - lift, down: since < 0.12, ring: since < 0.5 ? since / 0.5 : null };
  }

  /** Put every CSS animation (spinners, shimmer, pulses) at time t. */
  function syncAnimations(t) {
    for (const a of document.getAnimations()) { a.pause(); a.currentTime = t * 1000; }
  }

  return { clamp, ease, seg, lerp, morph, cursorAt, syncAnimations };
})();
