// VPN provider catalog for Connections: the app's presets and guides
// (routeTypes.ts), their logos, and how each provider's locations work.
import { TOP_VPN_PRESETS, type VpnPreset } from "./routeTypes";
import type { RouteProfile } from "../../../api";

const LOGOS: Record<string, string> = (() => {
  const out: Record<string, string> = {};
  const modules = import.meta.glob("../../assets/vpn/*.png", { eager: true, query: "?url", import: "default" });
  for (const [path, url] of Object.entries(modules)) {
    const m = /\/([^/]+)\.png$/.exec(path);
    if (m) out[m[1]] = url as string;
  }
  return out;
})();

/** Logos that are already a full square brand tile render edge to edge; the rest sit on a neutral tile. */
const FULL_BLEED = new Set(["ivpn", "expressvpn", "warp"]);

/**
 * How a provider's locations are reached. "any" = one WireGuard key works on
 * every server (gluetun ships their server lists: AirVPN, IVPN, Mullvad,
 * NordVPN, Proton, Surfshark, Windscribe; PIA via its own API).
 */
export type LocationMode = "any" | "configs" | "single" | "adapter" | "exit" | "tor";
const MODES: Record<string, LocationMode> = {
  mullvad: "any", ivpn: "any", nordvpn: "any", proton: "any", surfshark: "any", windscribe: "any", airvpn: "any", pia: "any",
  ovpn: "configs", cyberghost: "configs", ipvanish: "configs", warp: "single", expressvpn: "adapter", tailscale: "exit", tor: "tor",
};
export const MODE_TEXT: Record<LocationMode, string> = {
  any: "One key works for every location",
  configs: "One config per location",
  single: "One nearest-edge endpoint",
  adapter: "Uses the ExpressVPN app",
  exit: "Your Tailscale exit nodes",
  tor: "Tor SOCKS proxy",
};

export interface Provider extends VpnPreset {
  logo?: string;
  fullBleed: boolean;
  mode: LocationMode;
}

export const PROVIDERS: Provider[] = TOP_VPN_PRESETS.map((p) => ({
  ...p, logo: LOGOS[p.id], fullBleed: FULL_BLEED.has(p.id), mode: MODES[p.id] ?? "configs",
}));
export const providerById = (id: string) => PROVIDERS.find((p) => p.id === id);

/** Imported WireGuard locations are saved as route profiles named wg-<provider>-<location>. */
export const wireguardRouteId = (providerId: string, location: string) =>
  `wg-${providerId}-${location.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "location"}`;

export function providerOfRoute(route: RouteProfile): string | null {
  if (route.kind === "direct") return "direct";
  if (route.kind !== "wireguard" && route.kind !== "system_vpn") return null;
  const m = /^wg-([a-z0-9]+)-/.exec(route.id);
  if (m && providerById(m[1])) return m[1];
  return "custom";
}

export const isProxyRoute = (route: RouteProfile) => ["http_proxy", "socks5", "docker_socks5"].includes(route.kind);

/** Short human name for a route: "Direct", "Mullvad VPN · Stockholm", or a proxy address. */
export function routeLabel(route: RouteProfile | undefined): string {
  if (!route) return "Direct";
  if (route.kind === "direct") return "Direct";
  if (isProxyRoute(route)) return route.endpoint?.replace(/^\w+:\/\//, "") ?? route.id;
  const provider = providerById(providerOfRoute(route) ?? "");
  return provider ? `${provider.name.replace(/ \(.*\)$/, "")} · ${route.region || route.id}` : route.region || route.id;
}
