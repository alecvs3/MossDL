// Every captcha the engine has seen, newest first, on the shared column
// system: what asked, the site, how it ended, who solved it and how long it took.
import React, { useEffect, useMemo, useState } from "react";
import { getCaptchaHistory, type CaptchaHistoryEntry, type CaptchaOutcome } from "../../../api";
import { emitLog } from "../../../lib/telemetry";
import { CaptchaVendorLogo, InspectButton, OUTCOME_STYLE, OutcomePill, fmtDuration, fmtWhen } from "../../components/activity";
import { ColumnCells, ColumnHeader, TableScroll, useColumnLayout, type ColumnDef } from "../../ui/ColumnHeader";
import { SiteIcon } from "../../ui/SiteIcon";

const COLUMNS_KEY = "nexload.columns.captchas.v1";
const COLUMNS: ColumnDef[] = [
  { id: "type", title: "Captcha", minSize: 180, flex: true },
  { id: "site", title: "Site", size: 150, minSize: 90, maxSize: 280 },
  { id: "outcome", title: "Outcome", size: 88, minSize: 70, maxSize: 140 },
  { id: "solver", title: "Solved by", size: 120, minSize: 70, maxSize: 240 },
  { id: "took", title: "Took", size: 64, minSize: 48, maxSize: 120, align: "right" },
  { id: "task", title: "Download", size: 180, minSize: 90, maxSize: 400, hiddenByDefault: true },
  { id: "when", title: "When", size: 80, minSize: 60, maxSize: 160, align: "right" },
];
const FILTERS: Array<"all" | Exclude<CaptchaOutcome, "pending">> = ["all", "solved", "failed", "stalled", "skipped"];

export function CaptchaHistoryTab({ refreshKey }: { refreshKey: string }) {
  const [entries, setEntries] = useState<CaptchaHistoryEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const layout = useColumnLayout(COLUMNS_KEY, COLUMNS);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      if (document.hidden) return;
      try {
        const rows = await getCaptchaHistory(200);
        if (!cancelled) { setEntries(rows); setError(null); }
      } catch (e) {
        void emitLog("WARNING", "ui:captchas", "Captcha history failed to load", {}, e);
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    };
    void load();
    // New captchas also arrive through refreshKey; this catches outcomes that change later.
    const timer = window.setInterval(() => void load(), 60_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [refreshKey]);

  const shown = useMemo(() => (filter === "all" ? entries : entries.filter((e) => e.outcome === filter)), [entries, filter]);
  const count = (f: CaptchaOutcome) => entries.filter((e) => e.outcome === f).length;

  return (
    <>
      <div className="cp-body" style={{ flex: "0 0 auto", paddingBottom: 8 }}>
        <div className="cp-filters" role="group" aria-label="Show">
          {FILTERS.map((f) => (
            <button key={f} type="button" aria-pressed={filter === f} className={`cp-filter${filter === f ? " on" : ""}`} onClick={() => setFilter(f)}>
              {f}{f !== "all" && <span className="n" style={{ color: OUTCOME_STYLE[f].color }}>{count(f)}</span>}
            </button>
          ))}
        </div>
        {error && <div role="alert" className="cp-error" style={{ marginTop: 8 }}>Couldn't load the history: {error}</div>}
      </div>
      <TableScroll layout={layout} header={<ColumnHeader layout={layout} />} className="cp-table px-3 pb-2" role="grid" aria-label="Captcha history">
        {shown.length === 0 && (
          <div className="ui-empty-table"><p>{filter === "all" ? "No captchas yet. They show up here once a host asks for one." : `No ${filter} captchas.`}</p></div>
        )}
        {shown.map((entry) => (
          <div key={entry.id} className="hist-card" style={{ "--hist-accent": OUTCOME_STYLE[entry.outcome].color } as React.CSSProperties}>
            <div className="dt-row py-1.5" role="row">
              <ColumnCells layout={layout} cells={{
                type: (
                  <span className="cp-cell-main" title={entry.error ?? undefined}>
                    <CaptchaVendorLogo vendor={entry.vendor} size={18} />
                    <span className="t">{entry.type_label}</span>
                    {entry.error && entry.outcome !== "solved" && <span className="e" style={{ color: OUTCOME_STYLE[entry.outcome].color }}>{entry.error}</span>}
                  </span>
                ),
                site: entry.host ? (
                  <span className="cp-cell-main"><SiteIcon host={entry.host} size={14} /><span className="cp-muted">{entry.host}</span>
                    <InspectButton url={entry.page_url} label="Open the page" compact /></span>
                ) : <span className="cp-muted">—</span>,
                outcome: <OutcomePill outcome={entry.outcome} />,
                solver: <span className="cp-muted">{entry.outcome === "solved" ? entry.solver_label || "—" : "—"}</span>,
                took: <span className="cp-muted">{fmtDuration(entry.duration_seconds)}</span>,
                task: <span className="cp-muted" title={entry.task_name ?? undefined}>{entry.task_name || "—"}</span>,
                when: <span className="cp-muted">{fmtWhen(entry.created_at)}</span>,
              }} />
            </div>
          </div>
        ))}
      </TableScroll>
    </>
  );
}
