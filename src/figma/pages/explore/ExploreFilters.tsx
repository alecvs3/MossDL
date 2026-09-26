// Explore's right-hand filter column: name filter, kind, hosts (several at
// once), size, grouping. Sections collapse; Reset clears every filter.
import React, { useState } from "react";
import { Icon, ic } from "../../icons";
import { KINDS } from "../../../lib/fileKinds";
import { KindTile } from "../../ui/KindTile";
import { HostIcon } from "./ExploreRow";
import { initialView, type GroupBy, type SizeBucket, type ViewState } from "./exploreView";

const SIZES: [SizeBucket, string][] = [["any", "Any size"], ["small", "Small (< 1 MB)"], ["medium", "Medium (1–100 MB)"], ["large", "Large (> 100 MB)"]];
const GROUPS: [GroupBy, string][] = [["site", "Site"], ["source", "Page"], ["kind", "File type"], ["host", "Host"]];

function Section({ id, title, children, action }: { id: string; title: string; children: React.ReactNode; action?: React.ReactNode }) {
  const [open, setOpen] = useState(true);
  return (
    <div role="group" aria-labelledby={`xp-f-${id}`} className="xp-fsec">
      <div className="xp-fh">
        <button type="button" id={`xp-f-${id}`} className="xp-fh-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          {title}
          <span className={`xp-fh-chev${open ? " open" : ""}`} aria-hidden="true"><Icon d={ic.chevronDown} size={10} /></span>
        </button>
        {action}
      </div>
      {open && children}
    </div>
  );
}

export function ExploreFilters({ view, counts, onChange, queryRef }: {
  view: ViewState;
  counts: { kinds: Record<string, number>; hosts: Record<string, number> };
  onChange: (patch: Partial<ViewState>) => void;
  queryRef: React.RefObject<HTMLInputElement | null>;
}) {
  const [hostQuery, setHostQuery] = useState("");
  const kinds = ["all", ...Object.keys(KINDS).filter((k) => counts.kinds[k])];
  const hosts = Object.entries(counts.hosts)
    .filter(([h]) => !hostQuery || h.includes(hostQuery.toLowerCase()))
    .sort((a, b) => b[1] - a[1]);
  const changed = view.kind !== "all" || view.hosts.length > 0 || view.query !== "" || view.size !== "any" || view.groupBy !== initialView.groupBy;

  return (
    <aside className="xp-filters" aria-label="Filters">
      <div className="xp-filters-head">
        <span><Icon d={ic.sliders} size={12} /> Filters</span>
        <button type="button" className="xp-fh-clear" disabled={!changed}
          onClick={() => { onChange({ kind: "all", hosts: [], query: "", size: "any", groupBy: initialView.groupBy }); setHostQuery(""); }}>
          Reset
        </button>
      </div>
      <label className="xp-filter-q">
        <Icon d={ic.search} size={12} />
        <input
          ref={queryRef}
          value={view.query}
          onChange={(e) => onChange({ query: e.target.value })}
          onKeyDown={(e) => { if (e.key === "Escape" && view.query) { e.stopPropagation(); onChange({ query: "" }); } }}
          placeholder="Filter names"
          aria-label="Filter by name"
        />
        <kbd>Ctrl F</kbd>
      </label>

      <Section id="kind" title="File type">
        {kinds.map((k) => (
          <button key={k} type="button" className={`xp-fi${view.kind === k ? " on" : ""}`} aria-pressed={view.kind === k}
            onClick={() => onChange({ kind: k })}>
            {k === "all" ? <span className="xp-all-tile" aria-hidden="true"><Icon d={ic.layers} size={11} /></span> : <KindTile category={k} size={16} />}
            <span className="truncate">{k === "all" ? "All files" : KINDS[k].label}</span>
            <span className="xp-count">{counts.kinds[k] ?? 0}</span>
          </button>
        ))}
      </Section>

      {Object.keys(counts.hosts).length > 0 && (
        <Section id="host" title="Host"
          action={view.hosts.length > 0 ? <button type="button" className="xp-fh-clear" onClick={() => onChange({ hosts: [] })}>Clear</button> : undefined}>
          {Object.keys(counts.hosts).length > 6 && (
            <label className="xp-filter-q small">
              <Icon d={ic.search} size={11} />
              <input value={hostQuery} onChange={(e) => setHostQuery(e.target.value)} placeholder="Filter hosts" aria-label="Filter hosts" />
            </label>
          )}
          {hosts.map(([host, n]) => {
            const on = view.hosts.includes(host);
            return (
              <button key={host} type="button" className={`xp-fi${on ? " on" : ""}`} role="checkbox" aria-checked={on}
                title="Pick one or more hosts"
                onClick={() => onChange({ hosts: on ? view.hosts.filter((h) => h !== host) : [...view.hosts, host] })}>
                <span className={`xp-mini-check${on ? " on" : ""}`} aria-hidden="true" />
                <HostIcon host={host} size={14} />
                <span className="truncate">{host}</span>
                <span className="xp-count">{n}</span>
              </button>
            );
          })}
        </Section>
      )}

      <Section id="size" title="Size">
        <div role="radiogroup" aria-label="Size">
          {SIZES.map(([id, label]) => (
            <button key={id} type="button" role="radio" aria-checked={view.size === id} className={`xp-fi${view.size === id ? " on" : ""}`}
              onClick={() => onChange({ size: id })}>
              <span className={`xp-radio${view.size === id ? " on" : ""}`} aria-hidden="true" />
              <span className="truncate">{label}</span>
            </button>
          ))}
        </div>
      </Section>

      <Section id="group" title="Group by">
        <select className="xp-select" value={view.groupBy} onChange={(e) => onChange({ groupBy: e.target.value as GroupBy })} aria-label="Group by">
          {GROUPS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
        <button type="button" className="xp-fi" role="switch" aria-checked={view.showLow} onClick={() => onChange({ showLow: !view.showLow })}>
          <span className="truncate">Show low-confidence links</span>
          <span className={`xp-switch${view.showLow ? " on" : ""}`} aria-hidden="true" />
        </button>
      </Section>
    </aside>
  );
}
