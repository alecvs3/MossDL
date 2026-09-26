// Title cards and step captions over the demo: intro, the two interstitials
// between beats, the outro, and the numbered captions under the window.
window.TITLES = (() => {
  const { seg, ease } = E;
  const { letters, words, block } = TX;
  const LOGO = `${K.SRC}figma/assets/brand/mossdl-logo.svg`;

  function lockup(t, t0, t1, y, size, word) {
    const p = ease.back(Math.min(1, Math.max(0, (t - t0) / 0.7)));
    const out = seg(t, t1, t1 + 0.4, ease.in);
    const logo = `<img class="intro-logo" src="${LOGO}" alt="" style="top:${y}px;width:${size}px;height:${size}px;margin-left:${-size / 2}px;opacity:${Math.min(1, (t - t0) / 0.3) * (1 - out)};transform:scale(${0.72 + 0.28 * p - out * 0.08});filter:blur(${(1 - Math.min(1, (t - t0) / 0.4)) * 10 + out * 6}px) drop-shadow(0 18px 40px rgba(0,0,0,.55))">`;
    const o = { t0: t0 + 0.25, t1, per: 0.045, dur: 0.6, rise: 0.35, blur: 10 };
    const mark = `<div class="title intro-word" style="top:${y + size + 18}px;font-size:${word}px">${letters("Moss", t, o)}<span class="dl">${letters("DL", t, { ...o, t0: o.t0 + 0.18 })}</span></div>`;
    return logo + mark;
  }

  function intro(t, T) {
    if (t > T.introOut + 0.8) return "";
    return lockup(t, 0.05, T.introOut, 208, 132, 96)
      + `<div class="title mid" style="top:548px;font-size:44px">${words("Point it at a page. *Get* *the* *file.*", t, { t0: 0.95, t1: T.introOut, rise: 0.5, blur: 10 })}</div>`;
  }

  function interstitial(t, [a, b, one, two]) {
    if (t < a - 0.1 || t > b + 0.3) return "";
    const o = { t0: a + 0.12, t1: b - 0.38, per: 0.022, dur: 0.5, rise: 0.4, blur: 10 };
    return `<div class="title mid" style="top:352px">${letters(one, t, o)}</div>
      <div class="title mid em" style="top:432px">${letters(two, t, { ...o, t0: o.t0 + 0.22 })}</div>`;
  }

  function outro(t, T) {
    const t0 = T.outro + 0.45;
    if (t < t0) return "";
    const pills = `<div class="outro-pills" style="top:640px;${block(t, t0 + 1.2)}">${S_PILL("heart", "Free &amp; open source · GPL-3.0")}${S_PILL("windows", "Windows")}</div>`;
    return lockup(t, t0, 99, 170, 120, 92)
      + `<div class="title" style="top:500px;font-size:42px">${words("Point it at a page. *Get* *the* *file.*", t, { t0: t0 + 0.7, rise: 0.5, blur: 10 })}</div>` + pills;
  }

  const GLYPH = {
    heart: '<path d="M12 20s-7-4.35-7-10a4 4 0 0 1 7-2.65A4 4 0 0 1 19 10c0 5.65-7 10-7 10z"/>',
    windows: '<path d="M4 5.5l7-1v7H4zM13 4.2l7-1.2v8.5h-7zM4 13h7v7l-7-1zM13 13h7v8l-7-1.2z" fill="currentColor" stroke="none"/>',
  };
  const S_PILL = (g, text) => `<span class="pill"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">${GLYPH[g]}</svg>${text}</span>`;

  function caption(t, [a, b, n, text]) {
    if (t < a || t > b) return "";
    const p = Math.min(seg(t, a, a + 0.35, ease.out), 1 - seg(t, b - 0.35, b, ease.in));
    return `<div class="cap" style="opacity:${p.toFixed(3)};bottom:${(20 - (1 - p) * 14).toFixed(1)}px"><span class="cap-n">${n}</span><span>${words(text, t, { t0: a + 0.1, per: 0.04, dur: 0.45, rise: 0.4, blur: 6 })}</span></div>`;
  }

  return {
    titles: (t) => intro(t, TL.T) + TL.INTERSTITIALS.map((x) => interstitial(t, x)).join("") + outro(t, TL.T),
    captions: (t) => TL.CAPTIONS.map((c) => caption(t, c)).join(""),
  };
})();
