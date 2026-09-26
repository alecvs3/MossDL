import { useEffect, useMemo, useState } from "react";
import type { CollectionEnqueueResult, CollectionNode, CollectionPlan } from "../api";

export type CollectionPlanReviewProps = {
  plans: CollectionPlan[];
  activePlan?: CollectionPlan | null;
  defaultDestination: string;
  onOpen: (id: string) => Promise<CollectionPlan>;
  onSelect: (id: string, itemIds: string[], selected: boolean) => Promise<CollectionPlan>;
  onEnqueue: (id: string, itemIds: string[], destination: string) => Promise<CollectionEnqueueResult>;
  onCancel: (id: string) => Promise<CollectionPlan>;
  onContinue: (id: string) => Promise<CollectionPlan>;
};

function nodeKey(node: CollectionNode): string { return node.node_id; }

function outcomeLabel(value?: string | null): string {
  return String(value || "success").replaceAll("_", " ");
}

const actionableOutcomes = new Set(["partial_failure", "quota", "captcha", "login_required", "unsupported", "cycle", "canceled"]);

export default function CollectionPlanReview({ plans, activePlan, defaultDestination, onOpen, onSelect, onEnqueue, onCancel, onContinue }: CollectionPlanReviewProps) {
  const [plan, setPlan] = useState<CollectionPlan | null>(activePlan ?? plans[0] ?? null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [anchorId, setAnchorId] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [destination, setDestination] = useState(defaultDestination);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);

  useEffect(() => { setDestination(defaultDestination); }, [defaultDestination]);
  useEffect(() => { if (activePlan) setPlan(activePlan); }, [activePlan]);
  useEffect(() => { setSelectedIds((ids) => ids.filter((id) => plan?.items.some((item) => item.stable_id === id) ?? false)); }, [plan]);

  const visibleNodes = useMemo(() => {
    const query = filter.trim().toLowerCase();
    if (!plan) return [];
    if (!query) return plan.nodes;
    const matching = new Set(plan.nodes.filter((node) => `${node.display_name} ${node.folder_path ?? ""} ${node.package_path ?? ""}`.toLowerCase().includes(query)).map(nodeKey));
    const ancestorIds = new Set<string>();
    let changed = true;
    while (changed) {
      changed = false;
      plan.nodes.forEach((node) => {
        if (matching.has(nodeKey(node)) && node.parent_id && !ancestorIds.has(node.parent_id)) {
          ancestorIds.add(node.parent_id);
          changed = true;
        }
      });
    }
    ancestorIds.forEach((id) => matching.add(id));
    return plan.nodes.filter((node) => matching.has(nodeKey(node)));
  }, [filter, plan]);
  const itemIds = new Set(plan?.items.map((item) => item.stable_id) ?? []);

  const selectNode = (node: CollectionNode, event: React.MouseEvent) => {
    const id = nodeKey(node);
    const index = visibleNodes.findIndex((candidate) => nodeKey(candidate) === id);
    setFocusedId(id);
    if (event.shiftKey && anchorId) {
      const anchor = visibleNodes.findIndex((candidate) => nodeKey(candidate) === anchorId);
      if (anchor >= 0) setSelectedIds(visibleNodes.slice(Math.min(anchor, index), Math.max(anchor, index) + 1).filter((candidate) => itemIds.has(nodeKey(candidate))).map(nodeKey));
    } else if (event.ctrlKey || event.metaKey) {
      if (itemIds.has(id)) setSelectedIds((ids) => ids.includes(id) ? ids.filter((value) => value !== id) : [...ids, id]);
    } else setSelectedIds(itemIds.has(id) ? [id] : []);
    setAnchorId(id);
  };

  const submit = async () => {
    if (!plan || selectedIds.length === 0) return;
    setBusy(true);
    try {
      const updated = await onSelect(plan.id, selectedIds, true);
      setPlan(updated);
      const result = await onEnqueue(plan.id, selectedIds, destination);
      setOutcome(`${result.queued.length} ordinary task${result.queued.length === 1 ? "" : "s"} queued`);
      setPlan(await onOpen(plan.id));
      setSelectedIds([]);
    } catch (error) {
      setOutcome(error instanceof Error ? error.message : "Could not enqueue selected items");
      try {
        setPlan(await onOpen(plan.id));
      } catch (refreshError) {
        setOutcome(`${error instanceof Error ? error.message : "Could not enqueue selected items"}; refresh failed: ${refreshError instanceof Error ? refreshError.message : "engine unavailable"}`);
      }
    } finally { setBusy(false); }
  };

  return <section className="flex flex-col min-h-0 h-full p-5 gap-4" aria-label="Collection plan review">
    <header className="flex items-start justify-between gap-4 shrink-0"><div><p className="eyebrow">COLLECTIONS</p><h2 className="text-lg font-semibold">Review crawl hierarchy</h2><p className="subtitle">The engine owns discovery, outcomes, and task lifecycle. Select explicit resolved items to enqueue.</p></div><span className="figma-heading-stat"><strong>{plan?.items.length ?? 0}</strong><span>items</span></span></header>
    <div className="flex flex-wrap items-end gap-3 shrink-0">
      <label className="flex flex-col gap-1 text-xs text-[var(--text-muted)]">Collection<select className="win-select" value={plan?.id ?? ""} onChange={(event) => void onOpen(event.target.value).then(setPlan)}><option value="">Choose a collection</option>{plans.map((entry) => <option key={entry.id} value={entry.id}>{entry.id}</option>)}</select></label>
      <label className="flex flex-col gap-1 text-xs text-[var(--text-muted)]">Filter<input className="win-input" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Name or folder" /></label>
      <label className="flex flex-col gap-1 text-xs text-[var(--text-muted)]">Destination<input className="win-input" value={destination} onChange={(event) => setDestination(event.target.value)} /></label>
      <button className="btn-accent px-3 py-2 rounded-md text-xs" disabled={!plan || !selectedIds.length || busy} onClick={() => void submit()}>Enqueue selected ({selectedIds.length})</button>
      {plan?.has_more && <button className="px-3 py-2 rounded-md text-xs border border-[var(--border)]" disabled={busy} onClick={() => void onContinue(plan.id).then(setPlan)}>Continue crawl</button>}
      {plan && plan.state !== "canceled" && <button className="px-3 py-2 rounded-md text-xs border border-[var(--border)]" disabled={busy} onClick={() => void onCancel(plan.id).then(setPlan)}>Cancel crawl</button>}
    </div>
    {plan && <div className="rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2 text-xs" role="status">Outcome: <strong>{outcomeLabel(plan.outcome)}</strong> · state: {plan.state} · revision {plan.graph_revision}{outcome ? ` · ${outcome}` : ""}</div>}
    {plan?.events?.length ? <div className="text-xs text-[var(--warning)]">{plan.events.map((event, index) => { const value = String(event.outcome ?? "success"); return <span key={index} className="mr-3">{actionableOutcomes.has(value) ? "Action needed: " : ""}{outcomeLabel(value)}</span>; })}</div> : null}
    <div className="flex-1 min-h-0 overflow-auto space-y-1">{!plan && <div className="figma-empty-state"><strong>No collections</strong><span>Import or crawl a collection before reviewing it.</span></div>}{plan && visibleNodes.map((node) => { const selected = selectedIds.includes(nodeKey(node)); const isItem = itemIds.has(nodeKey(node)); return <div key={nodeKey(node)} role="treeitem" aria-selected={selected} style={{ paddingLeft: `${Math.min(node.depth, 8) * 20 + 8}px` }} tabIndex={0} onClick={(event) => selectNode(node, event)} className={`rounded-md border px-3 py-2 flex items-center gap-3 cursor-pointer ${selected ? "border-[var(--accent-light)] bg-[var(--accent-soft)]" : "border-[var(--border)] bg-[var(--surface-1)] hover:bg-[var(--surface-2)]"}`}><span className="text-xs">{isItem ? "↳" : "▸"}</span><span className="min-w-0 flex-1"><strong className="block truncate text-sm">{node.display_name}</strong><span className="block truncate text-xs text-[var(--text-muted)]">{node.folder_path || node.package_path || "root"} · {node.status || "discovered"}{node.outcome ? ` · ${outcomeLabel(node.outcome)}` : ""}</span></span><code className="text-[10px] text-[var(--text-muted)]">{node.node_id.slice(0, 8)}</code></div>; })}</div>
  </section>;
}
