// Loads the app's real stylesheets and the kit, in order, for any page in
// docs/brand/<folder>/. Pages set window.__ready once they have painted.
(() => {
  const src = "../../../src/figma/";
  const kit = "../kit/";
  const css = [`${src}index.css`, `${src}ui/controls.css`, `${src}pages/explore/explore.css`, `${src}pages/routes/connections.css`,
    `${src}pages/captchas/captchas.css`, `${kit}mock.css`, `${kit}brand.css`, `${kit}cards.css`];
  const js = ["icons.js", "data.js", "topo.js", "ui/atoms.js", "ui/shell.js", "ui/downloads.js", "ui/explore.js", "ui/connections.js", "ui/history.js", "ui/tree.js", "ui/cards.js"];
  document.write(css.map((h) => `<link rel="stylesheet" href="${h}">`).join("") + js.map((s) => `<script src="${kit}${s}"></script>`).join(""));

  window.params = new URLSearchParams(location.search);
  /** Mark the page ready once fonts and every image have loaded. */
  window.markReady = async () => {
    await document.fonts.ready;
    await Promise.all([...document.images].map((img) => (img.complete ? null : new Promise((r) => { img.onload = img.onerror = r; }))));
    requestAnimationFrame(() => requestAnimationFrame(() => { window.__ready = true; }));
  };
})();
