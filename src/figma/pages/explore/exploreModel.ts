// Explore's tree, derived entirely from engine results plus id-keyed UI state.
// Nothing here fetches or simulates: crawls, shortlink resolutions, browser
// captures and pasted links come from the engine; this turns them into rows.
import type {
  CaptureBatch,
  CaptureCandidate,
  CrawlPageMatrixResponse,
  LinkGrabberEntry,
  MatrixElement,
  ShortlinkResolutionResult,
} from "../../../api";
import { displayKind, extensionOf } from "../../../lib/fileKinds.ts";

export type NodeType = "site" | "source" | "group" | "button" | "step" | "folder" | "link" | "file";
export type Tone = "ok" | "warn" | "info" | "danger" | "muted";

export type Origin =
  | { kind: "matrix"; element: MatrixElement; crawlId: string }
  | { kind: "capture"; batchId: string; candidate: CaptureCandidate; index: number }
  | { kind: "entry"; entry: LinkGrabberEntry }
  | { kind: "crawl"; crawlId: string }
  | { kind: "none" };

export interface ExploreNode {
  id: string;
  type: NodeType;
  label: string;
  sub?: string;
  /** What would be downloaded or opened for this row. */
  url?: string;
  host?: string;
  kind?: string;
  ext?: string;
  size?: number | null;
  sizeText?: string;
  match?: number;
  best?: boolean;
  via?: "Explored" | "Captured" | "Pasted";
  meta?: string;
  tag?: { tone: Tone; text: string };
  /** The engine can follow this row further (resolve or explore it). */
  followable?: boolean;
  /** Engine work in flight for this row. */
  busy?: string;
  low?: boolean;
  /** Contains a high-scoring download: opened by default, everything else starts collapsed. */
  hot?: boolean;
  /** Shown open even without a high score (e.g. the posts on an index page). */
  openByDefault?: boolean;
  /** Capture batches merged into this page row (removing the row dismisses them all). */
  batchIds?: string[];
  /** When the engine found it (ms since epoch). */
  addedAt?: number;
  /** Rows under a site header; set when the view groups by site. */
  virtual?: boolean;
  children: ExploreNode[];
  origin: Origin;
}

export interface CrawlRecord {
  id: string;
  url: string;
  /** Row this crawl was started from, when following a link. */
  parentId?: string;
  response?: CrawlPageMatrixResponse;
  error?: string;
  loading: boolean;
  headless: boolean;
  startedAt: number;
}

export interface ExploreData {
  crawls: CrawlRecord[];
  resolutions: Record<string, ShortlinkResolutionResult>;
  resolveErrors: Record<string, string>;
  resolving: Set<string>;
  batches: CaptureBatch[];
  entries: LinkGrabberEntry[];
}

export const hostOf = (url?: string | null): string => {
  if (!url) return "";
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
};
/**
 * The site a URL belongs to: its registrable domain ("rutrk.org" for
 * "v3.rutrk.org"), keeping one more label for two-level country suffixes
 * such as "co.uk" or "com.au".
 */
export function siteOf(url?: string | null): string {
  const host = hostOf(url) || (url ?? "");
  const labels = host.split(".").filter(Boolean);
  if (labels.length <= 2) return labels.join(".");
  const [second, tld] = labels.slice(-2);
  const twoLevel = tld.length === 2 && ["co", "com", "net", "org", "ac", "gov", "edu", "ne", "or"].includes(second);
  return labels.slice(twoLevel ? -3 : -2).join(".");
}

/** File name from a URL's path (never its host), or "" when the path has none. */
const leafName = (url: string): string => {
  let path = url;
  try { path = new URL(url).pathname; } catch { path = url.split(/[?#]/)[0]; }
  let leaf = path.split("/").filter(Boolean).pop() ?? "";
  try { leaf = decodeURIComponent(leaf); } catch { /* keep the raw segment: it is still a usable name */ }
  return /\.[a-z0-9]{1,8}$/i.test(leaf) ? leaf : "";
};
const shortUrl = (url: string): string => url.replace(/^https?:\/\/(www\.)?/, "");
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

const node = (id: string, type: NodeType, label: string, origin: Origin, extra: Partial<ExploreNode> = {}): ExploreNode =>
  ({ id, type, label, origin, children: [], ...extra });

/* ── Crawl results ─────────────────────────────────────────── */

function elementNode(el: MatrixElement, crawl: CrawlRecord, data: ExploreData): ExploreNode {
  const id = `m:${crawl.id}:${el.id}`;
  const origin: Origin = { kind: "matrix", element: el, crawlId: crawl.id };
  const resolution = data.resolutions[id];
  const url = resolution?.final_url || el.target_url;
  const kind = displayKind(el.file_kind);
  const fileName = el.filename_hint || leafName(url);
  const host = hostOf(url) || el.host;
  const common = { url, host, match: Math.round(el.score), sizeText: el.size_hint || undefined };
  const busy = data.resolving.has(id) ? "Following…" : undefined;
  const error = data.resolveErrors[id];

  if (resolution) {
    const steps = resolution.hops.map((hop, i) =>
      node(`${id}/hop:${i}`, "step", shortUrl(hop.output_url || hop.input_url), { kind: "none" }, {
        url: hop.output_url, host: hostOf(hop.output_url), sub: hop.strategy.replace(/_/g, " "),
        tag: hop.captcha_encountered ? { tone: "warn", text: "captcha" } : hop.error ? { tone: "danger", text: hop.error } : undefined,
      }));
    // Button-following names the file itself (Content-Disposition); a stop
    // short of a file says why instead.
    const stopped = resolution.error && !resolution.direct_url;
    const finalName = stopped ? "" : resolution.filename || leafName(resolution.final_url);
    const finalNode = node(`${id}/final`, finalName ? "file" : "link", finalName || shortUrl(resolution.final_url), origin, {
      ...common, kind: finalName ? displayKind(el.file_kind !== "other" ? el.file_kind : undefined) : undefined,
      ext: extensionOf(finalName), followable: !finalName,
    });
    return node(id, el.tag !== "a" ? "button" : "link", el.text || shortUrl(el.target_url), origin, {
      ...common, sub: shortUrl(el.target_url), children: [...steps, finalNode],
      tag: resolution.captcha ? { tone: "warn", text: "captcha: follow in browser" }
        : stopped ? { tone: "danger", text: resolution.error! }
        : { tone: "ok", text: `${plural(resolution.hops.length, "step")}` },
    });
  }
  if (fileName && kind !== "other") {
    return node(id, "file", fileName, origin, { ...common, kind, ext: extensionOf(fileName), busy });
  }
  const isButton = el.tag !== "a";
  return node(id, isButton ? "button" : "link", isButton ? `“${el.text || "Download"}”` : el.text || shortUrl(url), origin, {
    ...common,
    sub: isButton ? `${el.tag}${el.method === "POST" ? " · form post" : ""}` : shortUrl(url),
    // Buttons are always followable: without a URL the follower starts from their page.
    followable: isButton || (Boolean(el.target_url) && !el.target_url.startsWith("javascript:")),
    busy,
    tag: error ? { tone: "danger", text: "couldn't follow" } : el.is_shortlink ? { tone: "info", text: "shortlink" } : undefined,
  });
}

/** Words that mark site navigation rather than content. */
const NAV_TEXT = /^(home|about|contact|login|log in|register|sign ?(in|up)|search|category|categories|tag|tags|faq|dmca|privacy|terms|vip|discord|next|prev(ious)?|page \d+|\d+|more|read more)$/i;

/** A link to another post or page on the same site, worth following. */
function isPostLink(el: MatrixElement, site: string): boolean {
  if (el.tag !== "a" || !el.target_url || siteOf(el.target_url) !== site) return false;
  const text = (el.text || "").trim();
  if (text.length < 8 || NAV_TEXT.test(text)) return false;
  try {
    const path = new URL(el.target_url).pathname;
    return path.length > 1 && !/\/(category|tag|author|page|search|feed|wp-|tdb_)/i.test(path);
  } catch {
    return false;
  }
}

function crawlChildren(crawl: CrawlRecord, data: ExploreData): ExploreNode[] {
  const res = crawl.response;
  if (!res) return [];
  const detected: ExploreNode[] = [];
  const buttons: ExploreNode[] = [];
  const posts: ExploreNode[] = [];
  const other: ExploreNode[] = [];
  const packages = new Map<string, ExploreNode>();
  const site = siteOf(crawl.url);

  for (const el of res.elements) {
    const n = elementNode(el, crawl, data);
    if (el.is_multipart && (el.package_name || el.multipart_base)) {
      const key = el.package_name || el.multipart_base!;
      const pkg = packages.get(key) ?? node(`${crawl.id}/pkg:${key}`, "folder", key, { kind: "none" }, { kind: displayKind(el.file_kind) });
      pkg.children.push(n);
      packages.set(key, pkg);
      continue;
    }
    // Detected = the engine rated it highly, or it is a real file the engine scored as likely.
    const useful = el.category === "high_utility" || (n.type === "file" && el.category !== "secondary" && el.score >= 60);
    if (useful) detected.push(n);
    else if (n.type === "button" || el.is_shortlink || el.category === "candidate") buttons.push(n);
    else if (isPostLink(el, site)) {
      // Index pages link each post twice (thumbnail and title): keep one, with the longer name.
      const twin = posts.findIndex((p) => p.url === n.url);
      if (twin < 0) posts.push(n);
      else if (n.label.length > posts[twin].label.length) posts[twin] = n;
    }
    else other.push({ ...n, low: true });
  }
  for (const pkg of packages.values()) {
    pkg.sub = plural(pkg.children.length, "part");
    pkg.match = Math.max(...pkg.children.map((c) => c.match ?? 0));
    detected.unshift(pkg);
  }

  // The single most promising download is starred; if nothing was detected,
  // the best candidates are still shown first rather than an empty tree.
  const ranked = [...detected, ...buttons].sort((a, b) => (b.match ?? 0) - (a.match ?? 0));
  if (ranked[0]) ranked[0].best = true;

  const group = (key: string, label: string, items: ExploreNode[], extra: Partial<ExploreNode> = {}) =>
    items.length ? [node(`${crawl.id}/g:${key}`, "group", label, { kind: "none" }, { children: items, ...extra })] : [];
  return [
    ...group("detected", detected.length ? "Detected downloads" : "Most promising", detected.length ? detected : buttons.slice(0, 5)),
    ...group("buttons", "Possible buttons", detected.length ? buttons : buttons.slice(5)),
    // An index or listing page has no downloads of its own, only posts that do:
    // show those open so the next step (double-click to explore one) is obvious.
    ...group("posts", detected.length || buttons.length ? "Pages on this site" : "Pages on this site — open one to find its downloads", posts,
      { openByDefault: !detected.length, low: detected.length > 0 }),
    ...group("other", "Other links", other, { low: true }),
  ];
}

function crawlNode(crawl: CrawlRecord, data: ExploreData): ExploreNode {
  const res = crawl.response;
  const meta = res
    ? [plural(res.total_elements, "link"), res.ads_stripped ? `${res.ads_stripped} ads removed` : "", res.used_headless ? "opened in browser" : "",
       res.has_countdown_timer ? "has a countdown" : ""].filter(Boolean).join(" · ")
    : undefined;
  return node(`crawl:${crawl.id}`, "source", res?.title || hostOf(crawl.url) || crawl.url, { kind: "crawl", crawlId: crawl.id }, {
    sub: shortUrl(crawl.url), url: crawl.url, host: hostOf(crawl.url), via: "Explored", meta,
    busy: crawl.loading ? (crawl.headless ? "Opening in browser…" : "Exploring…") : undefined,
    tag: crawl.error ? { tone: "danger", text: crawl.error } : undefined,
    children: crawlChildren(crawl, data),
  });
}

/* ── Browser captures and pasted links ─────────────────────── */

/** High enough that a row is worth showing without being asked. */
export const HOT_MATCH = 85;

/**
 * One row per captured page: the extension sends a batch per request burst,
 * so the same page arrives many times. Requests are merged by address, and
 * the ones the engine marked as noise (bot challenges, analytics, ads) go into
 * a collapsed "Other requests" group instead of competing with real files.
 */
function captureNodes(batches: CaptureBatch[]): { at: number; node: ExploreNode }[] {
  const pages = new Map<string, CaptureBatch[]>();
  for (const b of batches) {
    const key = b.page_url || b.page_origin;
    pages.set(key, [...(pages.get(key) ?? []), b]);
  }
  return [...pages].map(([page, group]) => {
    const byUrl = new Map<string, ExploreNode>();
    const noise: ExploreNode[] = [];
    for (const batch of group) {
      batch.candidates.forEach((c, index) => {
        const name = c.filename || leafName(c.url) || shortUrl(c.url);
        const n = node(`c:${batch.batch_id}:${c.candidate_id ?? index}`, "file", name, { kind: "capture", batchId: batch.batch_id, candidate: c, index }, {
          url: c.url, host: hostOf(c.url), kind: displayKind(c.file_kind), ext: extensionOf(name) || (c.mime?.split("/")[1] ?? ""),
          size: c.size ?? null, match: c.confidence != null ? Math.round(c.confidence * 100) : undefined,
          tag: c.noise ? { tone: "muted", text: c.noise } : undefined,
          addedAt: batch.created_at * 1000,
        });
        if (c.noise) { n.low = true; noise.push(n); return; }
        const prev = byUrl.get(c.url);
        if (!prev || (n.match ?? 0) > (prev.match ?? 0)) byUrl.set(c.url, n);
      });
    }
    const files = [...byUrl.values()].sort((x, y) => (y.match ?? 0) - (x.match ?? 0));
    if (files[0]) files[0].best = true;
    const children = noise.length
      ? [...files, node(`capture:${page}/noise`, "group", "Other requests", { kind: "none" }, { children: noise, low: true })]
      : files;
    const imported = group.every((b) => b.state === "imported");
    return {
      at: Math.max(...group.map((b) => b.created_at)) * 1000,
      node: node(`capture:${page}`, "source", hostOf(page) || "Browser capture", { kind: "none" }, {
        sub: shortUrl(page), url: page, host: hostOf(page), via: "Captured", children,
        batchIds: group.map((b) => b.batch_id),
        meta: `${plural(files.length, "file")}${noise.length ? ` · ${noise.length} other` : ""} from your browser`,
        tag: imported ? { tone: "ok", text: "imported" } : undefined,
      }),
    };
  });
}

/** Rows without their own time take the time their page was found. */
function inheritAddedAt(n: ExploreNode, at: number | undefined): void {
  if (n.addedAt == null) n.addedAt = at;
  else if (n.type === "source") at = n.addedAt;
  for (const c of n.children) inheritAddedAt(c, n.addedAt ?? at);
}

/** Siblings with the same name get their distinguishing path as a hint. */
function disambiguate(n: ExploreNode): void {
  const byLabel = new Map<string, ExploreNode[]>();
  for (const c of n.children) if (c.url) byLabel.set(c.label, [...(byLabel.get(c.label) ?? []), c]);
  for (const same of byLabel.values()) {
    if (same.length < 2) continue;
    for (const c of same) {
      if (c.sub) continue;
      try {
        const u = new URL(c.url!);
        const dirs = u.pathname.split("/").filter(Boolean).slice(0, -1);
        c.sub = `${u.hostname.replace(/^www\./, "")}/${dirs.slice(-2).join("/")}${u.search ? " " + u.search.slice(0, 24) : ""}`;
      } catch { c.sub = c.url; }
    }
  }
}

/** Marks every node that is, or contains, a high-scoring real download. */
function markHot(n: ExploreNode): boolean {
  const self = !n.low && n.type !== "group" && n.type !== "source" && (n.match ?? 0) >= HOT_MATCH;
  let child = false;
  for (const c of n.children) if (markHot(c) && !c.low) child = true;
  n.hot = self || child;
  return n.hot;
}

function entryNodes(entries: LinkGrabberEntry[]): ExploreNode[] {
  const bySource = new Map<string, LinkGrabberEntry[]>();
  for (const e of entries) {
    const key = e.source_context || e.source || "Pasted links";
    bySource.set(key, [...(bySource.get(key) ?? []), e]);
  }
  return [...bySource].map(([source, list]) =>
    node(`entries:${source}`, "source", source === "clipboard" ? "From clipboard" : source, { kind: "none" }, {
      via: "Pasted", meta: plural(list.length, "link"),
      children: list.map((e) => {
        const url = e.normalized_url || e.url;
        const name = e.title || leafName(url);
        const kind = displayKind(e.file_kind);
        return node(`e:${e.id}`, kind !== "other" ? "file" : "link", name || shortUrl(url), { kind: "entry", entry: e }, {
          url, host: hostOf(url), kind: kind !== "other" ? kind : undefined, ext: extensionOf(name), addedAt: e.created_at * 1000,
          tag: e.state === "failed" ? { tone: "danger", text: e.error || "failed" } : undefined,
        });
      }),
    }));
}

/** Walks every node, including ones attached during the walk. */
export function walk(nodes: ExploreNode[], visit: (n: ExploreNode, parent: ExploreNode | null) => void, parent: ExploreNode | null = null) {
  for (const n of nodes) {
    visit(n, parent);
    walk(n.children, visit, n);
  }
}

/** Newest first: captures and explored pages interleave by time. */
export function buildTree(data: ExploreData): ExploreNode[] {
  const roots: { at: number; node: ExploreNode }[] = [];
  const rootTimes = new Map<string, number>();
  const followed = new Map<string, ExploreNode[]>();
  for (const crawl of data.crawls) {
    const n = crawlNode(crawl, data);
    if (crawl.parentId) followed.set(crawl.parentId, [...(followed.get(crawl.parentId) ?? []), n]);
    else roots.push({ at: crawl.startedAt, node: n });
  }
  roots.push(...captureNodes(data.batches));
  roots.forEach((r) => rootTimes.set(r.node.id, r.at));
  const tree = [...roots.sort((a, b) => b.at - a.at).map((r) => r.node), ...entryNodes(data.entries)];
  // A crawl started by following a row hangs under that row, so a multi-step
  // path (page → button → mirror page → file) reads top to bottom.
  if (followed.size) walk(tree, (n) => { const kids = followed.get(n.id); if (kids) n.children.push(...kids); });
  tree.forEach(markHot);
  for (const root of tree) inheritAddedAt(root, root.addedAt ?? rootTimes.get(root.id));
  walk(tree, disambiguate);
  return tree;
}
