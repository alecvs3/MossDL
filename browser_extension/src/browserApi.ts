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
    alarms: api.alarms,
    connectNative: (hostName: string) => api.runtime.connectNative(hostName),
    extensionOrigin: () => String(api.runtime.getURL("")).replace(/\/$/, ""),
    addListener: (event: any, listener: (...args: any[]) => void, ...extra: any[]) => event?.addListener?.(listener, ...extra),
  };
}

/** chrome namespace callbacks work in Chrome and Firefox; consume lastError exactly once. */
export function callApi(adapter: Record<string, any>, owner: any, method: string, ...args: any[]): Promise<any> {
  return new Promise((resolve,reject)=> {
    if(typeof owner?.[method] !== 'function') { reject(new Error(`Browser capability unavailable: ${method}`)); return; }
    try {
      owner[method](...args,(value:any)=> {
        const error=adapter.runtime?.lastError;
        if(error) reject(new Error(error.message || `Browser ${method} failed`));
        else resolve(value);
      });
    } catch(error) { reject(error); }
  });
}

export function detectBrowserTarget(): "chrome" | "edge" | "firefox" {
  const root = globalThis as any;
  if (root.browser?.runtime?.getBrowserInfo) return "firefox";
  const ua = String(root.navigator?.userAgent ?? "").toLowerCase();
  return ua.includes("edg/") ? "edge" : "chrome";
}
