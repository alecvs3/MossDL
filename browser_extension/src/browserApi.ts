export const BROWSER_TARGETS = ["chrome", "edge", "firefox"] as const;

function namespace(): Record<string, any> {
  const root = globalThis as any;
  // Prefer chrome for callback-based compatibility (supported in both Chrome and Firefox),
  // falling back to browser (WebExtensions standard).
  const api = root.chrome ?? root.browser;
  if (!api) throw new Error("browser extension API is unavailable");
  return api;
}

export function createBrowserAdapter(target?: string): Record<string, any> {
  const api = namespace();
  const resolvedTarget = target ?? (api.runtime?.getBrowserInfo ? "firefox" : "chromium");
  if (resolvedTarget !== "chromium" && !BROWSER_TARGETS.includes(resolvedTarget as any)) {
    throw new Error(`unsupported browser adapter: ${resolvedTarget}`);
  }
  return {
    target: resolvedTarget,
    runtime: api.runtime,
    storage: api.storage,
    tabs: api.tabs,
    webRequest: api.webRequest,
    downloads: api.downloads,
    contextMenus: api.contextMenus,
    cookies: api.cookies,
    notifications: api.notifications,
    scripting: api.scripting,
    connectNative: (hostName: string) => api.runtime.connectNative(hostName),
    extensionOrigin: () => String(api.runtime.getURL("")).replace(/\/$/, ""),
    addListener: (event: any, listener: (...args: any[]) => void, ...extra: any[]) => event?.addListener?.(listener, ...extra),
  };
}

export function detectBrowserTarget(): "chrome" | "edge" | "firefox" {
  const root = globalThis as any;
  if (root.browser?.runtime?.getBrowserInfo) return "firefox";
  const ua = String(root.navigator?.userAgent ?? "").toLowerCase();
  return ua.includes("edg/") ? "edge" : "chrome";
}
