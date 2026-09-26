import React, { useState, useEffect, useMemo } from "react";
import { clampZoom, DEFAULT_ZOOM, nextZoom, calculateAutoWideScale } from "../../lib/zoom";
import type { LogLevel } from "../../api";
import { windowAction } from "../../api";
import type { AppSettings } from "../types";
import { rememberPaintedTheme } from "../themes";

export function useFigmaShellState(settings: AppSettings) {
  const [localSettings, setLocalSettings] = useState<AppSettings>(settings);
  const [devLogOpen, setDevLogOpen] = useState(false);
  const [devLogInitialLevel, setDevLogInitialLevel] = useState<LogLevel | "ALL">("ALL");
  const [viewportWidth, setViewportWidth] = useState(() =>
    typeof window !== "undefined" ? window.innerWidth : 1200
  );
  const [isMaximizedOrFullscreen, setIsMaximizedOrFullscreen] = useState(false);

  const handleToggleDevLogs = (level?: "ERROR" | "ALL") => {
    if (devLogOpen && (!level || level === devLogInitialLevel)) {
      setDevLogOpen(false);
    } else {
      if (level) setDevLogInitialLevel(level);
      setDevLogOpen(true);
    }
  };


  useEffect(() => {
    let timer: number | undefined;
    const applySize = () => {
      const w = window.innerWidth;
      const isMax =
        w >= screen.availWidth - 20 ||
        window.innerHeight >= screen.availHeight - 40 ||
        Boolean(document.fullscreenElement);
      setViewportWidth(w);
      setIsMaximizedOrFullscreen(isMax);
    };
    applySize();
    const handleResize = () => {
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(applySize, 80);
    };
    window.addEventListener("resize", handleResize);
    return () => {
      if (timer) window.clearTimeout(timer);
      window.removeEventListener("resize", handleResize);
    };
  }, []);

  const [zoom, setZoom] = useState(() => {
    try {
      return clampZoom(Number(localStorage.getItem("figma-zoom") || DEFAULT_ZOOM));
    } catch {
      return DEFAULT_ZOOM;
    }
  });


  const effectiveZoom = useMemo(() => {
    const configuredScale = localSettings.uiScale || "Auto";
    if (configuredScale !== "Auto") {
      const parsed = parseInt(configuredScale.replace("%", ""), 10);
      if (!isNaN(parsed) && parsed >= 75) {
        return clampZoom(parsed);
      }
    }
    const autoFactor = calculateAutoWideScale(
      viewportWidth,
      isMaximizedOrFullscreen,
      localSettings.autoFullscreenScale ?? true
    );
    const userOffset = zoom - 100;
    return clampZoom(autoFactor + userOffset);
  }, [localSettings.uiScale, localSettings.autoFullscreenScale, viewportWidth, isMaximizedOrFullscreen, zoom]);

  const [systemDark, setSystemDark] = useState(
    () => window.matchMedia?.("(prefers-color-scheme: dark)")?.matches ?? true
  );


  useEffect(() => {
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!media) return;
    const update = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);

  useEffect(() => setLocalSettings(settings), [settings]);

  useEffect(() => {
    localStorage.setItem("figma-zoom", String(zoom));
  }, [zoom]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        setZoom((value: number) => nextZoom(value, 10));
      } else if (event.key === "-" || event.key === "_" || event.key === "−") {
        event.preventDefault();
        setZoom((value: number) => nextZoom(value, -10));
      } else if (event.key === "0") {
        event.preventDefault();
        setZoom(DEFAULT_ZOOM);
      } else if (event.key === "`" || event.key === "~") {
        event.preventDefault();
        setDevLogOpen((prev) => !prev);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const colorMode = String(localSettings.colorMode).toLowerCase();
  // A theme is a whole palette. "system" (and the legacy Light/Dark colour mode)
  // resolve to the default light or dark theme.
  const theme = useMemo(() => {
    const named = String(localSettings.themeName || "").toLowerCase();
    if (named === "light") return "daylight"; // the light theme's old name
    if (named && named !== "system") return named;
    const prefersLight = colorMode === "light" || (colorMode === "system" && !systemDark);
    return prefersLight ? "daylight" : "midnight";
  }, [localSettings.themeName, colorMode, systemDark]);


  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    // The stylesheet paints the theme now; drop the boot background and remember this one.
    document.documentElement.style.removeProperty("background-color");
    rememberPaintedTheme(theme);
    return () => {
      delete document.documentElement.dataset.theme;
    };
  }, [theme]);


  useEffect(() => {
    const language = String(localSettings.language).toLowerCase();
    document.documentElement.lang = language.startsWith("deutsch")
      ? "de"
      : language.startsWith("español")
      ? "es"
      : language.startsWith("français")
      ? "fr"
      : language.startsWith("日本詞")
      ? "ja"
      : "en-US";
  }, [localSettings.language]);

  // Only override the theme accent with a concrete colour. The unset default is
  // the string "var(--accent)", and `--accent: var(--accent)` is a cycle that
  // makes every var(--accent) below the shell resolve to nothing.
  const customAccent = /^#|^rgb|^hsl/i.test(localSettings.colorAccent ?? "") ? localSettings.colorAccent : null;
  const themeStyle = {
    ...(customAccent ? { "--accent": customAccent, "--accent-glow": `${customAccent}cc` } : {}),
    "--app-zoom": `${effectiveZoom / 100}`,
    "--font-family": localSettings.fontFamily
      ? `'${localSettings.fontFamily}', 'Segoe UI Variable Text', 'Segoe UI', system-ui, sans-serif`
      : "var(--font-ui)",
  } as React.CSSProperties;


  return {
    localSettings,
    setLocalSettings,
    devLogOpen,
    setDevLogOpen,
    devLogInitialLevel,
    setDevLogInitialLevel,
    handleToggleDevLogs,
    zoom,
    setZoom,
    effectiveZoom,
    theme,
    themeStyle,
  };
}
