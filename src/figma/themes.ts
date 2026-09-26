// The theme gallery (wizard and Settings) and the launch cache that lets the
// first paint use the saved theme instead of flashing the default one while
// the engine is still starting. Each palette itself lives in index.css.

export const THEMES = [
  { id: "midnight", name: "Midnight", description: "The default deep blue-black", accent: "#0078D4",
    preview: { base: "#0d0d14", surface: "#1b1b28", accent: "#4da6f5", success: "#34d399", warning: "#f59e0b" } },
  { id: "evergreen", name: "Evergreen", description: "Dark with a green signal", accent: "#2f9e68",
    preview: { base: "#101613", surface: "#1c2a22", accent: "#5ecb92", success: "#5ecb92", warning: "#e2b53f" } },
  { id: "graphite", name: "Graphite", description: "Neutral Windows 11 grey", accent: "#4cc2ff",
    preview: { base: "#1b1b1f", surface: "#2a2a30", accent: "#4cc2ff", success: "#6ccb5f", warning: "#fce100" } },
  { id: "indigo", name: "Indigo", description: "Cool blue-violet", accent: "#7c6cff",
    preview: { base: "#12121f", surface: "#22223a", accent: "#7c6cff", success: "#5ee0b0", warning: "#ffc95c" } },
  { id: "daylight", name: "Daylight", description: "Light, for bright rooms", accent: "#0078D4",
    preview: { base: "#f4f7fb", surface: "#ffffff", accent: "#0b68b5", success: "#12805c", warning: "#9a6700" } },
  { id: "system", name: "Match Windows", description: "Follow the system light/dark setting", accent: "#0078D4",
    preview: { base: "linear-gradient(135deg,#0d0d14 50%,#f4f7fb 50%)", surface: "#5b6474", accent: "#4da6f5", success: "#34d399", warning: "#f59e0b" } },
] as const;

const APPEARANCE_KEY = "mossdl-appearance";
// Read by public/boot-theme.js before any script or stylesheet loads.
const PAINT_KEY = "mossdl-boot-theme";

/** The appearance settings saved at the last launch, so the first render matches them. */
export function cachedAppearance(): Record<string, unknown> {
  try {
    const parsed = JSON.parse(localStorage.getItem(APPEARANCE_KEY) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (error) {
    console.warn("[THEME_CACHE] unreadable appearance cache; using defaults", error);
    return {};
  }
}

export function cacheAppearance(appearance: Record<string, unknown> | undefined): void {
  if (!appearance) return;
  try {
    localStorage.setItem(APPEARANCE_KEY, JSON.stringify(appearance));
  } catch (error) {
    console.warn("[THEME_CACHE] could not save the appearance cache", error);
  }
}

/** Remember the painted theme and its background for the next launch's first frame. */
export function rememberPaintedTheme(theme: string): void {
  try {
    const background = getComputedStyle(document.documentElement).getPropertyValue("--app-bg").trim();
    localStorage.setItem(PAINT_KEY, JSON.stringify({ theme, background }));
  } catch (error) {
    console.warn("[THEME_CACHE] could not save the painted theme", error);
  }
}
