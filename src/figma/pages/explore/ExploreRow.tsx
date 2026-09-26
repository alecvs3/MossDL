// One Explore row. Levels are told apart by shape, not by margin lines:
// site/page rows are bands with a favicon tile and a summary, groups are
// small headings, files are indented with a muted file-type tile.
import React from "react";
import { formatBytes } from "../../../lib/format";
import { Icon, ic } from "../../icons";
import { SiteIcon } from "../../ui/SiteIcon";
import { KindTile, SiteTile } from "../../ui/KindTile";
import { ColumnCells, type ColumnLayout } from "../../ui/ColumnHeader";
import type { ExploreNode } from "./exploreModel";
import type { CheckState, Row, summarize } from "./exploreView";

const TYPE_ICON: Partial<Record<ExploreNode["type"], string>> = {
  button: ic.cursor, step: ic.step, folder: ic.folder, link: ic.link,
};
const TYPE_WORD: Record<ExploreNode["type"], string> = {
  site: "Site", source: "Page", group: "Group", button: "Button", step: "Step", folder: "Folder", link: "Link", file: "File",
};
const INDENT = 18;

export function HostIcon({ host, size = 14 }: { host?: string; size?: number }) {
  return host ? <SiteIcon host={host} size={size} /> : null;
}

export function ago(ms?: number): string {
  if (!ms) return "";
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

export function ExploreRow({
  row, columns, check, open, focused, fresh, queued, summary, onToggleOpen, onCheck, onPrimary, onMenu,
}: {
  row: Row;
  columns: ColumnLayout;
  check: CheckState;
  open: boolean;
  focused: boolean;
  queued: boolean;
  /** Appeared while the page was open. */
  fresh: boolean;
  /** Totals for header rows (site, page, group). */
  summary?: ReturnType<typeof summarize>;
  onToggleOpen: () => void;
  onCheck: (e: React.MouseEvent) => void;
  onPrimary: () => void;
  onMenu: (e: React.MouseEvent) => void;
}) {
  const n = row.node;
  const header = n.type === "site" || n.type === "source";
  // A page under its site's header is named by its path; the site is already said.
  const pathLabel = n.type === "source" && row.depth > 0 && n.sub ? n.sub.replace(/^[^/]+\//, "/") : null;
  const title = pathLabel ?? n.label;
  const hasKids = n.children.length > 0 || Boolean(n.busy);
  const primaryLabel = n.followable && n.type !== "file" ? "Follow" : header ? "Download best match" : "Add to Downloads";
  const size = n.size ?? (header && summary?.size ? summary.size : null);
  const match = n.match ?? (header ? summary?.best : undefined);
  const label = [TYPE_WORD[n.type], n.label, size ? formatBytes(size) : n.sizeText, n.host, match ? `${match}% match` : "", queued ? "in Downloads" : ""]
    .filter(Boolean).join(", ");

  const icon = header ? (
    <SiteTile>{n.host ? <SiteIcon host={n.host} size={14} /> : <Icon d={ic.link} size={13} />}</SiteTile>
  ) : n.type === "group" ? null : n.type === "file" ? (
    <KindTile category={n.kind} />
  ) : (
    <span className="xp-ico" aria-hidden="true"><Icon d={TYPE_ICON[n.type] ?? ic.link} size={14} /></span>
  );

  return (
    <div
      id={`xr-${n.id}`}
      className={`dt-row xp-row xp-${n.type}${check === "on" ? " selected" : ""}${focused ? " focused" : ""}${n.low ? " low" : ""}${n.best ? " best" : ""}${fresh ? " fresh" : ""}${open ? " open" : ""}${row.depth ? " nested" : ""}`}
      role="row"
      aria-level={row.depth + 1}
      aria-expanded={hasKids ? open : undefined}
      aria-selected={check === "on"}
      aria-label={label}
      data-row-id={n.id}
    >
      <ColumnCells
        layout={columns}
        cells={{
          select: (
            <span
              className={`xp-check ${check}`}
              data-check
              onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); onCheck(e); }}
              title={check === "on" ? "Unselect" : "Select"}
              aria-hidden="true"
            />
          ),
          name: (
            <div className="xp-name" style={{ paddingLeft: row.depth * INDENT }}>
              <span
                className={`xp-chev${hasKids ? "" : " none"}${open ? " open" : ""}`}
                onMouseDown={(e) => { e.stopPropagation(); if (hasKids) onToggleOpen(); }}
                title={open ? "Collapse" : "Expand"}
                aria-hidden="true"
              >
                <Icon d={ic.chevronRight} size={10} />
              </span>
              {icon}
              <span className="xp-label" title={n.url ? `${title}\n${n.url}` : title}>{title}</span>
              {fresh && <span className="xp-new">New</span>}
              {n.best && <span className="xp-star" title="Best match" aria-label="Best match">★</span>}
              {header && summary && (
                <span className="xp-count-chip">
                  {summary.pages ? `${plural(summary.pages, "page")} · ` : ""}{plural(summary.files, "file")}
                </span>
              )}
              {n.type === "group" && <span className="xp-count-chip">{n.children.length}</span>}
              {n.via && n.type === "source" && <span className={`xp-tag${n.via === "Captured" ? " info" : ""}`}>{n.via}</span>}
              {queued && <span className="xp-tag ok"><Icon d={ic.check} size={9} /> In Downloads</span>}
              {n.tag && <span className={`xp-tag ${n.tag.tone}`} title={n.tag.text}>{n.tag.text}</span>}
              {n.sub && !pathLabel && <span className="xp-sub" title={n.sub}>{n.sub}</span>}
              {n.busy && <><span className="xp-spin" aria-hidden="true" /><span className="xp-sub">{n.busy}</span></>}
              {!n.busy && n.followable && n.type !== "file" && <span className="xp-hint">Double-click to follow</span>}
              {!n.busy && n.type === "file" && !queued && <span className="xp-hint">Double-click to download</span>}
            </div>
          ),
          kind: n.ext ? <span className="xp-kind">{n.ext}</span> : null,
          size: <span className="xp-num">{size ? formatBytes(size) : n.sizeText ?? ""}</span>,
          host: n.host && n.type !== "group" ? (
            <span className="xp-host"><HostIcon host={n.host} /><span className="truncate">{n.host}</span></span>
          ) : null,
          match: match ? (
            <span className={`xp-match${match >= 85 ? " hi" : ""}`} title={`${match}% match`}>
              <span className="xp-bar" aria-hidden="true"><span style={{ width: `${match}%` }} /></span>
              <b>{match}%</b>
            </span>
          ) : null,
          added: n.type === "group" ? null : <span className="xp-when" title={n.addedAt ? new Date(n.addedAt).toLocaleString() : undefined}>{ago(n.addedAt)}</span>,
          url: n.url ? <span className="xp-sub truncate" title={n.url}>{n.url}</span> : null,
          actions: n.type === "group" ? null : (
            <span className="xp-acts">
              <button type="button" className="xp-act-primary" tabIndex={-1} title={primaryLabel} aria-label={primaryLabel}
                onClick={(e) => { e.stopPropagation(); onPrimary(); }}>
                <Icon d={n.followable && n.type !== "file" ? ic.play : ic.download} size={12} />
              </button>
              <button type="button" className="xp-act-menu" tabIndex={-1} title="More actions (Shift+F10)" aria-label="More actions"
                onClick={(e) => { e.stopPropagation(); onMenu(e); }}>
                <Icon d={ic.dots} size={12} />
              </button>
            </span>
          ),
        }}
      />
    </div>
  );
}
