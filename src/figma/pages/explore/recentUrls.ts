// Recently explored links, for the ▾ next to Explore's address bar. A per-user
// convenience kept in local storage, like column widths.
const KEY = "nexload.explore.recent.v1";
const MAX = 12;

export function loadRecentUrls(): string[] {
  try {
    const raw = localStorage.getItem(KEY);
    const list = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(list) ? list.filter((x): x is string => typeof x === "string").slice(0, MAX) : [];
  } catch {
    return []; // unreadable or blocked storage just means no history
  }
}

export function rememberUrl(url: string): string[] {
  const next = [url, ...loadRecentUrls().filter((u) => u !== url)].slice(0, MAX);
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // a full or blocked storage quota must not stop exploring
  }
  return next;
}
