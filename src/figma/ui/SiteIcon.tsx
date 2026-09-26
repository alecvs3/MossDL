// A site's icon anywhere a download, link or host is shown. Order: the app's
// bundled provider logo, then the favicon the engine cached for that host,
// then a letter tile. Lookups from one render pass go to the engine together.
import React, { useEffect, useState } from "react";
import { getFavicons } from "../../api";
import { emitLog } from "../../lib/telemetry";
import { ProviderIcon, detectProvider, getBundledProviderIcon } from "../icons";

const known = new Map<string, string | null>();
const waiting = new Map<string, Set<(icon: string | null) => void>>();
let timer: number | undefined;

function flush() {
  timer = undefined;
  const hosts = [...waiting.keys()].filter((h) => !known.has(h));
  if (!hosts.length) return;
  const settle = (icons: Record<string, string | null>) => {
    for (const h of hosts) {
      const icon = icons[h] ?? null;
      known.set(h, icon);
      waiting.get(h)?.forEach((cb) => cb(icon));
      waiting.delete(h);
    }
  };
  getFavicons(hosts).then(settle, (error: unknown) => {
    void emitLog("WARNING", "ui:favicons", "Favicon lookup failed", { hosts }, error);
    // Not cached as a miss: the next render asks again.
    hosts.forEach((h) => waiting.delete(h));
  });
}

function request(host: string, cb: (icon: string | null) => void) {
  if (!waiting.has(host)) waiting.set(host, new Set());
  waiting.get(host)!.add(cb);
  if (timer === undefined) timer = window.setTimeout(flush, 30);
  return () => { waiting.get(host)?.delete(cb); };
}

export const hostOfUrl = (url?: string | null): string => {
  if (!url) return "";
  try { return new URL(url.includes("://") ? url : `https://${url}`).hostname.replace(/^www\./, "").toLowerCase(); } catch { return ""; }
};

export function SiteIcon({ url, host, size = 16 }: { url?: string | null; host?: string | null; size?: number }) {
  const h = (host || hostOfUrl(url)).replace(/^www\./, "").toLowerCase();
  const provider = h ? detectProvider(`https://${h}`) : "direct";
  const bundled = provider !== "direct" && Boolean(getBundledProviderIcon(provider));
  const [icon, setIcon] = useState<string | null | undefined>(() => (h ? known.get(h) : null));
  const [broken, setBroken] = useState(false);

  useEffect(() => {
    setBroken(false);
    if (!h || bundled) return;
    if (known.has(h)) { setIcon(known.get(h)); return; }
    setIcon(undefined);
    return request(h, setIcon);
  }, [h, bundled]);

  if (bundled) return <ProviderIcon provider={provider} size={size} />;
  if (icon && !broken) {
    return <img src={icon} alt="" width={size} height={size} draggable={false} onError={() => setBroken(true)}
      className="site-icon" style={{ width: size, height: size }} />;
  }
  return (
    <span className="site-letter" style={{ width: size, height: size, fontSize: Math.max(8, size * 0.56) }} aria-hidden="true">
      {h ? h[0].toUpperCase() : "?"}
    </span>
  );
}
