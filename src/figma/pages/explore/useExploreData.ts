// Engine work behind Explore: page crawls and following links further.
// Results are kept exactly as the engine returned them; the tree is derived.
import { useCallback, useRef, useState } from "react";
import { crawlPageMatrix, followDownload, resolveShortlink, type ShortlinkResolutionResult } from "../../../api";
import { emitLog } from "../../../lib/telemetry";
import type { CrawlRecord, ExploreNode } from "./exploreModel";

const message = (error: unknown) => (error instanceof Error ? error.message : String(error));

export function useExploreData() {
  const [crawls, setCrawls] = useState<CrawlRecord[]>([]);
  const [resolutions, setResolutions] = useState<Record<string, ShortlinkResolutionResult>>({});
  const [resolveErrors, setResolveErrors] = useState<Record<string, string>>({});
  const [resolving, setResolving] = useState<Set<string>>(new Set());
  const seq = useRef(0);

  const runCrawl = useCallback(async (id: string, url: string, headless: boolean) => {
    try {
      const response = await crawlPageMatrix(url, headless);
      setCrawls((all) => all.map((c) => (c.id === id ? { ...c, response, loading: false, error: undefined } : c)));
      return response;
    } catch (error) {
      void emitLog("WARNING", "ui:explore", "Crawl failed", { url, headless }, error);
      setCrawls((all) => all.map((c) => (c.id === id ? { ...c, loading: false, error: message(error) } : c)));
      return null;
    }
  }, []);

  // Mirror of `crawls` so explore() can decide the id synchronously.
  const crawlsRef = useRef<CrawlRecord[]>([]);
  crawlsRef.current = crawls;

  /** Explores a page. Exploring the same URL from the same place refreshes it in place. */
  const explore = useCallback((url: string, options: { headless?: boolean; parentId?: string } = {}) => {
    const headless = Boolean(options.headless);
    const existing = crawlsRef.current.find((c) => c.url === url && c.parentId === options.parentId);
    const id = existing?.id ?? `x${++seq.current}`;
    const record: CrawlRecord = { id, url, parentId: options.parentId, loading: true, headless, startedAt: Date.now() };
    const next = existing
      ? crawlsRef.current.map((c) => (c.id === id ? { ...c, loading: true, headless, error: undefined } : c))
      : [...crawlsRef.current, record];
    crawlsRef.current = next;
    setCrawls(next);
    return runCrawl(id, url, headless);
  }, [runCrawl]);

  /** Follows a row further, using whichever engine path fits it: the resolver
   *  for shortlinks and form posts, the browser for buttons (clicking through
   *  mirrors and countdowns to the file), a page crawl for plain links. */
  const follow = useCallback(async (n: ExploreNode, headless = false) => {
    const el = n.origin.kind === "matrix" ? n.origin.element : null;
    // A script-only button has no URL: start from its page; the follower finds it.
    const pageUrl = n.origin.kind === "matrix" ? crawlsRef.current.find((c) => c.id === (n.origin as { crawlId: string }).crawlId)?.url : undefined;
    const target = n.url || pageUrl;
    if (!target) return;
    const viaResolver = el && !n.id.endsWith("/final") && (el.is_shortlink || el.method === "POST") && !headless;
    const viaBrowser = !viaResolver && (headless || n.type === "button");
    if (!viaResolver && !viaBrowser) {
      await explore(target, { parentId: n.id, headless });
      return;
    }
    setResolving((s) => new Set(s).add(n.id));
    try {
      const result = viaBrowser ? await followDownload(target) : await resolveShortlink(el!.target_url, undefined, el!.method, el!.form_data);
      setResolutions((all) => ({ ...all, [n.id]: result }));
      setResolveErrors(({ [n.id]: _cleared, ...rest }) => rest);
    } catch (error) {
      void emitLog("WARNING", "ui:explore", viaBrowser ? "Following in the browser failed" : "Shortlink resolution failed", { url: target }, error);
      setResolveErrors((all) => ({ ...all, [n.id]: message(error) }));
    } finally {
      setResolving((s) => { const next = new Set(s); next.delete(n.id); return next; });
    }
  }, [explore]);

  const removeCrawl = useCallback((crawlId: string) => {
    setCrawls((all) => {
      // Dropping a page also drops everything that was followed from inside it.
      const doomed = new Set([crawlId]);
      const inside = (parentId: string) => [...doomed].some((d) => parentId === `crawl:${d}` || parentId.includes(`:${d}:`) || parentId.startsWith(`${d}/`));
      for (let grew = true; grew;) {
        grew = false;
        for (const c of all) {
          if (!doomed.has(c.id) && c.parentId && inside(c.parentId)) { doomed.add(c.id); grew = true; }
        }
      }
      return all.filter((c) => !doomed.has(c.id));
    });
  }, []);

  return { crawls, resolutions, resolveErrors, resolving, explore, follow, removeCrawl };
}
