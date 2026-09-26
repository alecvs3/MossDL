// Engine state and actions behind Connections. Route profiles, health checks
// and proxy tests all come from the engine; nothing here invents a status.
import { useCallback, useEffect, useState } from "react";
import {
  checkRouteHealth, deleteRouteProfile, getTunnelStatus, listRouteProfiles, nextHealthyRoute, putSecret,
  providerAccountRefresh, providerAccountSignIn, providerAccountSignOut, saveRouteProfile, saveWireguardRoute, testProxyDownload, type RouteProfile, type TunnelStatus,
} from "../../../api";
import { emitLog } from "../../../lib/telemetry";
import { toast, toastUndo } from "../../ui/Toasts";
import { proxyEndpoint, proxyRouteId, type ParsedProxy } from "./proxyParse";
import { wireguardRouteId } from "./providers";

export interface Health {
  healthy: boolean; checkedAt: number; ip?: string | null; error?: string | null; latencyMs?: number | null; checking?: boolean;
  /** A WireGuard route's tunnel in the core, as the core reports it. */
  tunnel?: TunnelStatus | null;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function useConnections(initial: RouteProfile[]) {
  const [routes, setRoutes] = useState<RouteProfile[]>(initial);
  const [health, setHealth] = useState<Record<string, Health>>({});

  const reload = useCallback(async () => {
    try { setRoutes(await listRouteProfiles()); }
    catch (e) { void emitLog("WARNING", "ui:connections", "Could not list routes", {}, e); toast(`Couldn't load connections: ${message(e)}`, { tone: "danger" }); }
  }, []);
  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => { if (initial.length) setRoutes(initial); }, [initial]);

  const mark = (id: string, patch: Partial<Health>) =>
    setHealth((all) => ({ ...all, [id]: { ...(all[id] ?? { healthy: false, checkedAt: 0 }), ...patch } }));

  /** WireGuard/VPN/direct routes: the engine's route health check. */
  const check = useCallback(async (route: RouteProfile) => {
    mark(route.id, { checking: true });
    try {
      if (["http_proxy", "socks5", "docker_socks5"].includes(route.kind)) {
        const r = await testProxyDownload(route.endpoint ?? "", route.kind === "http_proxy" ? "http_proxy" : "socks5", route.credential_ref);
        mark(route.id, { checking: false, healthy: r.ok, checkedAt: Date.now(), ip: r.egress_ip, latencyMs: r.latency_ms, error: r.ok ? null : r.error });
      } else {
        const r = await checkRouteHealth(route.id);
        const tunnel = route.kind === "wireguard" && route.tunnel !== "system" ? (await getTunnelStatus())[route.id] ?? null : null;
        mark(route.id, { checking: false, healthy: r.healthy, checkedAt: r.checked_at ? r.checked_at * 1000 : Date.now(),
          ip: r.public_ip, error: r.error, latencyMs: (r as { latency_ms?: number }).latency_ms ?? null, tunnel });
      }
    } catch (e) {
      void emitLog("WARNING", "ui:connections", "Route check failed", { route: route.id }, e);
      mark(route.id, { checking: false, healthy: false, checkedAt: Date.now(), error: message(e) });
    }
  }, []);

  const checkMany = useCallback(async (list: RouteProfile[]) => {
    // A few at a time: checks are network probes, not free.
    const queue = [...list];
    const worker = async () => { for (let r = queue.shift(); r; r = queue.shift()) await check(r); };
    await Promise.all([worker(), worker(), worker(), worker()]);
  }, [check]);

  const setEnabled = useCallback(async (route: RouteProfile, enabled: boolean) => {
    await saveRouteProfile({ ...route, enabled });
    await reload();
  }, [reload]);

  /** Hides the routes now and deletes them (and their stored keys) once the
   *  Undo window closes; Undo just shows them again. */
  const remove = useCallback((list: RouteProfile[], label: string) => {
    const ids = new Set(list.map((r) => r.id));
    setRoutes((all) => all.filter((r) => !ids.has(r.id)));
    toastUndo(`Removed ${label}`, () => {
      void (async () => {
        for (const r of list) {
          try { await deleteRouteProfile(r.id); }
          catch (e) { void emitLog("WARNING", "ui:connections", "Route delete failed", { route: r.id }, e); toast(`Couldn't remove ${r.region || r.id}: ${message(e)}`, { tone: "danger" }); }
        }
        await reload();
      })();
    }, () => void reload());
  }, [reload]);

  /** The whole .conf goes to the engine, which keeps the keys in the secret
   *  store; the tunnel then runs inside the app, no WireGuard app needed. */
  const importWireguard = useCallback(async (providerId: string, conf: string, location: string) => {
    const id = wireguardRouteId(providerId, location);
    await saveWireguardRoute(id, conf, location);
    const fresh = await listRouteProfiles();
    setRoutes(fresh);
    return fresh.find((r) => r.id === id) ?? null;
  }, []);

  /** Saves pasted proxies; logins go to the secret store, never into the route. */
  const importProxies = useCallback(async (list: ParsedProxy[]) => {
    const ids: string[] = [];
    for (const p of list) {
      const id = proxyRouteId(p);
      const credential_ref = p.username ? (await putSecret(`proxy/${id}`, `${p.username}:${p.password ?? ""}`, "proxy")).credential_ref : null;
      await saveRouteProfile({ id, kind: p.kind, endpoint: proxyEndpoint(p), region: null, enabled: true, credential_ref });
      ids.push(id);
    }
    const fresh = await listRouteProfiles();
    setRoutes(fresh);
    return fresh.filter((r) => ids.includes(r.id));
  }, []);

  /** Account sign-in / refresh / sign-out; each reloads the locations the engine made. */
  const account = useCallback(async (action: "signIn" | "refresh" | "signOut", provider: string, accountNumber = "") => {
    if (action === "signIn") await providerAccountSignIn(provider, accountNumber);
    else if (action === "refresh") await providerAccountRefresh(provider);
    else await providerAccountSignOut(provider);
    await reload();
  }, [reload]);

  const nextBest = useCallback(async (current: string | null) => (await nextHealthyRoute(current)).route, []);

  return { routes, health, reload, check, checkMany, setEnabled, remove, importWireguard, importProxies, account, nextBest };
}
