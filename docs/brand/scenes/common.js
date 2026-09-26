// Helpers shared by the still scenes (social, hero, feature cards).
window.S = (() => {
  const LOGO = `${K.SRC}figma/assets/brand/mossdl-logo.svg`;
  const GLYPH = {
    heart: '<path d="M12 20s-7-4.35-7-10a4 4 0 0 1 7-2.65A4 4 0 0 1 19 10c0 5.65-7 10-7 10z"/>',
    windows: '<path d="M4 5.5l7-1v7H4zM13 4.2l7-1.2v8.5h-7zM4 13h7v7l-7-1zM13 13h7v8l-7-1.2z" fill="currentColor" stroke="none"/>',
    rust: '<path d="M12 3l2 2.5 3-.8.6 3.1 3 1.1-1.3 2.9 1.3 2.9-3 1.1-.6 3.1-3-.8L12 21l-2-2.5-3 .8-.6-3.1-3-1.1L4.7 12 3.4 9.1l3-1.1.6-3.1 3 .8z"/><circle cx="12" cy="12" r="3.2"/>',
  };
  const glyph = (name, size = 14) => GLYPH[name]
    ? `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${GLYPH[name]}</svg>`
    : icon(name, size);

  return {
    glyph,
    lockup: (size = 60, word = 42) => `<div class="lockup"><img src="${LOGO}" alt="" style="width:${size}px;height:${size}px"><span class="wordmark" style="font-size:${word}px">Moss<b>DL</b></span></div>`,
    checks: (items) => items.map((t) => `<li><span class="tick">${icon("check", 14)}</span>${t}</li>`).join(""),
    pill: (g, text) => `<span class="pill">${glyph(g, 14)}${text}</span>`,
    /** The Downloads list at a moment mid-session. */
    downloads: (theme) => KIT_DATA.downloads.map((d, i) => ({ ...d, seed: i * 1.7 + 1, via: d.host === "pixeldrain.com" ? "Pixeldrain" : d.host === "archive.org" ? "Internet Archive" : "Direct" })),
    /** Tall, readable text for small feature facts. */
    fact: (ic, title, text) => `<div class="fact"><span class="fact-ic">${icon(ic, 16)}</span><div><b>${title}</b><span>${text}</span></div></div>`,
  };
})();
