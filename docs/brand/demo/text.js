// Kinetic type: letters and words that rise into place and leave, computed
// from time only (no CSS transitions), so every frame is reproducible.
window.TX = (() => {
  const { clamp, ease } = E;
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");

  /** Visibility 0..1 of item i: enters from `t0` with a stagger, leaves from `t1`. */
  function vis(t, i, n, { t0, t1 = Infinity, per = 0.03, dur = 0.55, outPer = 0.012, outDur = 0.35 }) {
    const inP = ease.out(clamp((t - t0 - i * per) / dur));
    const outP = ease.in(clamp((t - t1 - (n - 1 - i) * outPer) / outDur));
    return { inP, outP };
  }
  const style = ({ inP, outP }, rise = 0.45, blur = 8) =>
    `opacity:${(inP * (1 - outP)).toFixed(3)};transform:translateY(${((1 - inP) * rise - outP * rise * 0.8).toFixed(3)}em);filter:blur(${((1 - inP) * blur + outP * blur * 0.6).toFixed(2)}px)`;

  /** Letters of `text`, each in its own span. */
  function letters(text, t, o) {
    const chars = [...text];
    return chars.map((c, i) => c === " " ? " " : `<span class="kl" style="${style(vis(t, i, chars.length, o), o.rise, o.blur)}">${esc(c)}</span>`).join("");
  }

  /** Words of `text` (keeps HTML-free words; wrap highlights with *stars*). */
  function words(text, t, o) {
    const list = text.split(" ");
    return list.map((w, i) => {
      const em = /^\*.*\*[.,]?$/.test(w);
      const word = em ? w.replace(/\*/g, "") : w;
      return `<span class="kw${em ? " em" : ""}" style="${style(vis(t, i, list.length, { per: 0.07, dur: 0.6, ...o }), o.rise, o.blur)}">${esc(word)}</span>`;
    }).join(" ");
  }

  /** Whole-block fade used for small print and lockups. */
  const block = (t, t0, t1 = Infinity, dur = 0.5) => style(vis(t, 0, 1, { t0, t1, dur, outDur: 0.35 }), 0.6, 6);

  return { letters, words, block };
})();
