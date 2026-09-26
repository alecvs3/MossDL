// Remembers when each row first appeared, so rows that arrive while the page
// is open (new captures, results of following a link) can be marked "New".
// Rows present when the page opens are not new.
import { useEffect, useRef, useState } from "react";
import { walk, type ExploreNode } from "./exploreModel";

export const NEW_FOR_MS = 10_000;

export function useFreshRows(tree: ExploreNode[]) {
  const firstSeen = useRef<Map<string, number> | null>(null);
  const roots = useRef(new Map<string, number>());
  const [, rerender] = useState(0);

  useEffect(() => {
    const now = Date.now();
    const initial = firstSeen.current === null;
    const seen = firstSeen.current ?? new Map<string, number>();
    let added = false;
    walk(tree, (n, parent) => {
      if (seen.has(n.id)) return;
      // Only the top of a new branch is marked; its descendants inherit nothing,
      // so a newly explored page shows one "New", not one per row.
      // (A crawl's rows arrive after its page row, so "new" parent means recently new.)
      const parentAt = parent ? seen.get(parent.id) ?? 0 : 0;
      const parentIsNew = parentAt > 0 && now - parentAt < NEW_FOR_MS;
      seen.set(n.id, initial ? 0 : now);
      if (!initial && !parentIsNew) { roots.current.set(n.id, now); added = true; }
    });
    firstSeen.current = seen;
    if (!added) return;
    // Repaint once the marker should fade.
    const timer = window.setTimeout(() => rerender((x) => x + 1), NEW_FOR_MS + 50);
    return () => window.clearTimeout(timer);
  }, [tree]);

  const now = Date.now();
  const isNew = (id: string) => now - (roots.current.get(id) ?? 0) < NEW_FOR_MS;
  let count = 0;
  roots.current.forEach((at) => { if (now - at < NEW_FOR_MS) count++; });
  return { isNew, count };
}
