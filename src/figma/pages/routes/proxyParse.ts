// Parses pasted proxy lists in the formats people actually have:
//   host:port · host:port:user:pass · user:pass@host:port · scheme://[user:pass@]host:port
//   JSON arrays of strings or {host|ip, port, type|protocol, username, password}
// Only kinds the engine can route are accepted; everything else is reported.

export type ProxyKind = "http_proxy" | "socks5";
export interface ParsedProxy {
  kind: ProxyKind;
  host: string;
  port: number;
  username?: string;
  password?: string;
  line: string;
}
export interface RejectedLine { line: string; reason: string }

const SCHEME: Record<string, ProxyKind | "unsupported"> = {
  http: "http_proxy", https: "http_proxy", socks5: "socks5", socks5h: "socks5", socks: "socks5",
  socks4: "unsupported", socks4a: "unsupported",
};

function validPort(value: string | number): number | null {
  const port = Number(value);
  return Number.isInteger(port) && port > 0 && port < 65536 ? port : null;
}
const HOST = /^(\[[0-9a-f:.]+\]|[a-z0-9.-]+)$/i;

function parseText(line: string, fallback: ProxyKind): ParsedProxy | RejectedLine {
  let rest = line.trim();
  let kind: ProxyKind = fallback;
  const scheme = /^([a-z0-9]+):\/\//i.exec(rest);
  if (scheme) {
    const mapped = SCHEME[scheme[1].toLowerCase()];
    if (!mapped) return { line, reason: `unknown type “${scheme[1]}”` };
    if (mapped === "unsupported") return { line, reason: "SOCKS4 isn't supported yet — use SOCKS5 or HTTP" };
    kind = mapped;
    rest = rest.slice(scheme[0].length).replace(/\/$/, "");
  }
  let username: string | undefined, password: string | undefined;
  const at = rest.lastIndexOf("@");
  if (at > 0) {
    const auth = rest.slice(0, at);
    rest = rest.slice(at + 1);
    const colon = auth.indexOf(":");
    if (colon < 0) return { line, reason: "login needs user:password" };
    username = auth.slice(0, colon); password = auth.slice(colon + 1);
  }
  // host:port or host:port:user:pass (IPv6 must be bracketed).
  const m = /^(\[[^\]]+\]|[^:\s]+):(\d+)(?::([^:\s]+):(\S+))?$/.exec(rest);
  if (!m) return { line, reason: "expected host:port" };
  const port = validPort(m[2]);
  if (!port) return { line, reason: `port ${m[2]} is out of range` };
  if (!HOST.test(m[1])) return { line, reason: `“${m[1]}” isn't a host name or address` };
  if (m[3]) { username = m[3]; password = m[4]; }
  return { kind, host: m[1], port, username, password, line };
}

function fromJson(value: unknown, fallback: ProxyKind): (ParsedProxy | RejectedLine)[] {
  const list = Array.isArray(value) ? value : [value];
  return list.map((item) => {
    if (typeof item === "string") return parseText(item, fallback);
    if (!item || typeof item !== "object") return { line: JSON.stringify(item), reason: "not a proxy" };
    const o = item as Record<string, unknown>;
    const host = String(o.host ?? o.ip ?? o.address ?? "");
    const typeName = String(o.type ?? o.protocol ?? o.scheme ?? "").toLowerCase();
    const scheme = typeName ? `${Array.isArray(o.protocols) ? String(o.protocols[0]) : typeName}://` : "";
    const auth = o.username ? `${o.username}:${o.password ?? ""}@` : "";
    return parseText(`${scheme}${auth}${host}:${o.port ?? ""}`, fallback);
  });
}

export function parseProxyList(text: string, fallback: ProxyKind = "http_proxy"): { proxies: ParsedProxy[]; rejected: RejectedLine[] } {
  const trimmed = text.trim();
  let results: (ParsedProxy | RejectedLine)[];
  if (trimmed.startsWith("[") || trimmed.startsWith("{")) {
    try {
      results = fromJson(JSON.parse(trimmed), fallback);
    } catch {
      results = [{ line: trimmed.slice(0, 60), reason: "looks like JSON but doesn't parse" }];
    }
  } else {
    results = trimmed.split(/\r?\n/).map((l) => l.trim()).filter((l) => l && !l.startsWith("#")).map((l) => parseText(l, fallback));
  }
  const seen = new Set<string>();
  const proxies: ParsedProxy[] = [];
  const rejected: RejectedLine[] = [];
  for (const r of results) {
    if ("reason" in r) { rejected.push(r); continue; }
    const key = `${r.kind}:${r.host}:${r.port}`;
    if (seen.has(key)) { rejected.push({ line: r.line, reason: "duplicate" }); continue; }
    seen.add(key);
    proxies.push(r);
  }
  return { proxies, rejected };
}

export const proxyEndpoint = (p: Pick<ParsedProxy, "kind" | "host" | "port">) =>
  `${p.kind === "socks5" ? "socks5" : "http"}://${p.host}:${p.port}`;
export const proxyRouteId = (p: Pick<ParsedProxy, "kind" | "host" | "port">) =>
  `px-${p.kind === "socks5" ? "s5" : "http"}-${p.host.replace(/[^a-z0-9]/gi, "-")}-${p.port}`;
