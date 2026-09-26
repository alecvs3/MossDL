// Groups the download links found on a web page so the Add window can offer
// "this game, 4 parts, from DataNodes" instead of 235 raw anchors. Release
// pages usually list the same files on several mirrors; picking every link
// would download everything once per mirror, so each group defaults to one.

export type PageLink = {
  id: string;
  text: string;
  target_url: string;
  host: string;
  category: "high_utility" | "candidate" | "secondary" | string;
  provider_id?: string | null;
  filename_hint?: string;
  size_hint?: string;
  is_shortlink?: boolean;
  is_multipart?: boolean;
  multipart_part?: number;
  multipart_total?: number;
  package_name?: string;
  unsupported?: boolean;
};

export type LinkMirror = {
  host: string;
  providerId: string | null;
  links: PageLink[];
};

export type LinkGroup = {
  key: string;
  name: string;
  isPackage: boolean;
  /** Only "candidate" links: likely files, but not the page's main download. */
  optional: boolean;
  mirrors: LinkMirror[];
  defaultMirror: string;
};

export type GroupedPageLinks = { groups: LinkGroup[]; other: PageLink[] };

const normalize = (value: string) => value.toLowerCase().replace(/[^a-z0-9]+/g, "");

function urlLeaf(url: string): string {
  try {
    const parsed = new URL(url);
    return decodeURIComponent(parsed.pathname.replace(/\/+$/, "").split("/").pop() || "");
  } catch {
    return "";
  }
}

/** Human filename for a link, or undefined when only the link text is known. */
export function linkFileName(link: PageLink): string | undefined {
  if (link.filename_hint && link.filename_hint.includes(".")) return link.filename_hint;
  const leaf = urlLeaf(link.target_url);
  return leaf.includes(".") ? leaf : undefined;
}

function groupKey(link: PageLink): { key: string; name: string; isPackage: boolean } | null {
  if (link.is_multipart && link.package_name) {
    return { key: `pkg:${normalize(link.package_name)}`, name: link.package_name, isPackage: true };
  }
  const file = linkFileName(link);
  if (file) return { key: `file:${normalize(file)}`, name: file, isPackage: false };
  return null;
}

/** Prefer mirrors a dedicated provider handles, then page order. */
function mirrorRank(mirror: LinkMirror, order: number): number {
  return (mirror.providerId && mirror.providerId !== "generic" ? 0 : 1000) + order;
}

export function groupPageLinks(links: PageLink[]): GroupedPageLinks {
  const groups = new Map<string, LinkGroup & { order: Map<string, number> }>();
  const other: PageLink[] = [];

  for (const link of links) {
    const downloadable = (link.category === "high_utility" || link.category === "candidate") && !link.unsupported;
    const identity = downloadable ? groupKey(link) : null;
    if (!identity) {
      other.push(link);
      continue;
    }
    let group = groups.get(identity.key);
    if (!group) {
      group = { ...identity, optional: true, mirrors: [], defaultMirror: "", order: new Map() };
      groups.set(identity.key, group);
    }
    if (link.category === "high_utility") group.optional = false;
    const host = link.host || "unknown";
    let mirror = group.mirrors.find((item) => item.host === host);
    if (!mirror) {
      mirror = { host, providerId: link.provider_id ?? null, links: [] };
      group.order.set(host, group.mirrors.length);
      group.mirrors.push(mirror);
    }
    // The same part listed twice on one mirror is one download.
    if (!mirror.links.some((existing) => existing.target_url === link.target_url)) mirror.links.push(link);
  }

  const result: LinkGroup[] = [];
  for (const { order, ...group } of groups.values()) {
    for (const mirror of group.mirrors) {
      mirror.links.sort((a, b) => (a.multipart_part ?? 0) - (b.multipart_part ?? 0));
    }
    const ranked = [...group.mirrors].sort((a, b) => mirrorRank(a, order.get(a.host) ?? 0) - mirrorRank(b, order.get(b.host) ?? 0));
    result.push({ ...group, defaultMirror: ranked[0]?.host ?? "" });
  }
  // Main downloads first, optional extras after.
  result.sort((a, b) => Number(a.optional) - Number(b.optional));
  return { groups: result, other };
}

/** Split pasted text into distinct http(s)/magnet links, keeping order. */
export function splitPastedLinks(text: string): string[] {
  // A new scheme starts a new link even without whitespace ("…/a.ziphttps://…"),
  // except where links legitimately embed one: after "=", "/", "?", "&" or "%"
  // (shortlinks like "?url=https://…" or "/go/https://…").
  const found = text.match(/(?:https?:\/\/|magnet:\?)(?:(?!(?<![=/?&%])(?:https?:\/\/|magnet:\?))[^\s<>"'])+/gi) || [];
  const seen = new Set<string>();
  const links: string[] = [];
  for (const raw of found) {
    const link = raw.replace(/[),.;!?]+$/, "");
    if (!seen.has(link)) {
      seen.add(link);
      links.push(link);
    }
  }
  // Links typed without a scheme: each dotted token goes to the engine, which
  // adds https:// or rejects it (engine/linkgrabber.py is the one parser).
  if (!links.length) {
    for (const token of text.split(/\s+/)) {
      const t = token.replace(/[),;!?]+$/, "");
      if (t.includes(".") && !seen.has(t)) { seen.add(t); links.push(t); }
    }
  }
  return links;
}
