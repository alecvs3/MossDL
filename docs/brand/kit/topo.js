// Topographic contour lines: the brand's background texture (mossy terrain,
// in place of the usual circuit traces). Deterministic for a given seed.
(() => {
  function rings({ cx, cy, count = 14, r0 = 40, gap = 34, seed = 1, squash = 0.72, tilt = 0 }) {
    const paths = [];
    for (let k = 0; k < count; k++) {
      const r = r0 + k * gap;
      const pts = [];
      for (let i = 0; i <= 96; i++) {
        const a = (i / 96) * Math.PI * 2;
        const wob = 1 + 0.09 * Math.sin(3 * a + seed + k * 0.23) + 0.05 * Math.sin(5 * a + seed * 2.1 - k * 0.17) + 0.025 * Math.sin(9 * a + seed * 0.7);
        const x = Math.cos(a) * r * wob, y = Math.sin(a) * r * wob * squash;
        pts.push([cx + x * Math.cos(tilt) - y * Math.sin(tilt), cy + x * Math.sin(tilt) + y * Math.cos(tilt)]);
      }
      paths.push({ d: "M" + pts.map((p) => p.map((v) => v.toFixed(1)).join(" ")).join("L") + "Z", k });
    }
    return paths;
  }

  /** An <svg> of contour rings around each given centre. */
  window.topo = (w, h, centres, { color = "rgba(182,236,108,1)", opacity = 0.09 } = {}) => {
    const all = centres.flatMap((c) => rings(c).map((p) => ({ ...p, n: c.count || 14 })));
    return `<svg class="topo" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid slice" fill="none">${all.map(({ d, k, n }) =>
      `<path d="${d}" stroke="${color}" stroke-opacity="${(opacity * (1 - (k / n) * 0.75)).toFixed(3)}" stroke-width="${k % 5 === 4 ? 1.3 : 0.8}"/>`).join("")}</svg>`;
  };
})();
