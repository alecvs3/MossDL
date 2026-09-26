// Runs before the app's scripts and styles: paint the saved theme's background
// so a relaunch never flashes the default theme. Written by src/figma/themes.ts.
(function () {
  try {
    var saved = JSON.parse(localStorage.getItem("mossdl-boot-theme") || "null") || {};
    var root = document.documentElement;
    if (saved.theme) root.dataset.theme = saved.theme;
    // Cleared once the stylesheet paints the theme itself (glass windows need it gone).
    root.style.backgroundColor = saved.background || "#0d0d14";
  } catch (error) {
    console.warn("[THEME_CACHE] boot theme unavailable", error);
  }
})();
