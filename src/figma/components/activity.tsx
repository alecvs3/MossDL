// Shared presentation for captcha, shortlink and per-download activity history.
// Used by the Captchas page and the History page so both read the same way.
import React from "react";
import {
  openPath,
  type CaptchaHistoryEntry,
  type CaptchaOutcome,
  type CaptchaVendor,
  type ShortlinkHistoryChain,
  type TaskTimeline,
  type TaskTimelineEvent,
  type TimelineTone,
} from "../../api";
import { Icon, ic } from "../icons";

export const OUTCOME_STYLE: Record<CaptchaOutcome, { label: string; color: string; bg: string; border: string }> = {
  solved: { label: "Solved", color: "var(--success)", bg: "rgba(52,211,153,0.12)", border: "rgba(52,211,153,0.3)" },
  failed: { label: "Failed", color: "var(--danger)", bg: "rgba(248,113,113,0.12)", border: "rgba(248,113,113,0.3)" },
  stalled: { label: "Stalled", color: "var(--warning)", bg: "rgba(251,191,36,0.12)", border: "rgba(251,191,36,0.3)" },
  skipped: { label: "Skipped", color: "var(--text-muted)", bg: "rgba(148,163,184,0.1)", border: "rgba(148,163,184,0.25)" },
  pending: { label: "Waiting", color: "var(--accent-light)", bg: "rgba(96,165,250,0.12)", border: "rgba(96,165,250,0.3)" },
};

const TONE_COLOR: Record<TimelineTone, string> = {
  success: "var(--success)",
  error: "var(--danger)",
  warning: "#fbbf24",
  neutral: "rgba(255,255,255,0.35)",
};

export function fmtDuration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function fmtWhen(epochSeconds?: number | null, now = Date.now() / 1000): string {
  if (!epochSeconds) return "—";
  const ago = now - epochSeconds;
  if (ago < 60) return "just now";
  if (ago < 3600) return `${Math.floor(ago / 60)}m ago`;
  if (ago < 86400) return `${Math.floor(ago / 3600)}h ago`;
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function fmtClock(epochSeconds?: number | null): string {
  if (!epochSeconds) return "";
  return new Date(epochSeconds * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Simplified brand marks so each captcha is recognisable at a glance. */
export function CaptchaVendorLogo({ vendor, size = 18 }: { vendor: CaptchaVendor; size?: number }) {
  const common = { width: size, height: size, viewBox: "0 0 24 24", "aria-hidden": true } as const;
  switch (vendor) {
    case "cloudflare":
      return (
        <svg {...common}>
          <title>Cloudflare</title>
          <path fill="#F38020" d="M16.9 16.5H5.2a3.2 3.2 0 0 1-.4-6.4 4.6 4.6 0 0 1 8.7-1.9 3.6 3.6 0 0 1 5.5 2.6 3 3 0 0 1-.2 5.7z" />
          <path fill="#FBAD41" d="M19.3 16.5h-2.1l.6-2.1a1.7 1.7 0 0 0-1.6-2.2h-.6a2.9 2.9 0 0 1 5.4 1.4 2.9 2.9 0 0 1-1.7 2.9z" />
        </svg>
      );
    case "google":
      return (
        <svg {...common}>
          <title>reCAPTCHA</title>
          <path fill="none" stroke="#4A90E2" strokeWidth="2.4" strokeLinecap="round" d="M19.5 12a7.5 7.5 0 0 1-12.8 5.3" />
          <path fill="none" stroke="#1C3AA9" strokeWidth="2.4" strokeLinecap="round" d="M4.5 12a7.5 7.5 0 0 1 12.8-5.3" />
          <path fill="#1C3AA9" d="M17.8 3.2v5.1h-5.1z" />
          <path fill="#4A90E2" d="M6.2 20.8v-5.1h5.1z" />
          <circle cx="12" cy="12" r="2" fill="#ABABAB" />
        </svg>
      );
    case "hcaptcha":
      return (
        <svg {...common}>
          <title>hCaptcha</title>
          <rect x="3" y="3" width="18" height="18" rx="4" fill="#0074BF" />
          <path fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" d="M9 7.5v9M15 7.5v9M9 12h6" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <title>Image captcha</title>
          <rect x="3" y="4" width="18" height="16" rx="3" fill="rgba(148,163,184,0.25)" stroke="#94a3b8" strokeWidth="1.5" />
          <path fill="none" stroke="#cbd5e1" strokeWidth="1.8" strokeLinecap="round" d="M7 14.5l2.5-3 2.5 3 2-2 3 3" />
          <circle cx="15.5" cy="8.5" r="1.4" fill="#cbd5e1" />
        </svg>
      );
  }
}

export function OutcomePill({ outcome }: { outcome: CaptchaOutcome }) {
  const style = OUTCOME_STYLE[outcome];
  return (
    <span
      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px] font-semibold whitespace-nowrap"
      style={{ color: style.color, background: style.bg, border: `1px solid ${style.border}` }}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: style.color }} />
      {style.label}
    </span>
  );
}

/** The ↗ "open in browser" affordance used wherever a page can be inspected. */
export function InspectButton({ url, label = "Inspect in browser", compact = false }: {
  url?: string | null;
  label?: string;
  compact?: boolean;
}) {
  if (!url) return null;
  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        void openPath(url);
      }}
      title={`${label}: ${url}`}
      aria-label={label}
      className={`inline-flex items-center gap-1 rounded-md text-[10.5px] font-medium text-sky-300 hover:text-white hover:bg-sky-500/20 transition-colors shrink-0 ${
        compact ? "p-1.5" : "px-2.5 py-1.5 bg-sky-500/10 border border-sky-400/20"
      }`}
    >
      <Icon d={ic.externalLink} size={compact ? 12 : 11} />
      {!compact && <span>Inspect</span>}
    </button>
  );
}

export function ShortlinkChainSteps({ chain }: { chain: ShortlinkHistoryChain }) {
  return (
    <div className="flex items-center flex-wrap gap-1">
      {chain.hops.map((hop, index) => {
        const failed = Boolean(hop.error) || hop.state === "failed";
        return (
          <React.Fragment key={hop.hop}>
            {index > 0 && <Icon d={ic.chevronRight} size={11} className="text-[var(--ink-25)]" />}
            <span
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono"
              style={{
                background: failed ? "rgba(248,113,113,0.12)" : "rgba(255,255,255,0.05)",
                color: failed ? "#fca5a5" : "rgba(255,255,255,0.7)",
              }}
              title={hop.error || hop.state || ""}
            >
              <span className="text-[var(--ink-30)]">{hop.hop + 1}</span>
              {hop.host || "unknown"}
            </span>
          </React.Fragment>
        );
      })}
    </div>
  );
}

export function ShortlinkHistoryList({ chains }: { chains: ShortlinkHistoryChain[] }) {
  if (chains.length === 0) {
    return <p className="text-[11px] text-[var(--ink-35)] px-1 py-4 text-center">No shortlinks unravelled yet.</p>;
  }
  return (
    <div className="rounded-xl border border-[var(--line-05)] overflow-hidden divide-y divide-[var(--line-05)]">
      {chains.map((chain) => (
        <div
          key={chain.task_id}
          className="px-3 py-2 bg-white/[0.015] space-y-1"
          style={{ boxShadow: `inset 3px 0 0 ${chain.failed ? "var(--danger)" : "var(--success)"}` }}
        >
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-[11px] text-[var(--ink-80)] truncate flex-1" title={chain.task_name || chain.task_id}>
              {chain.task_name || chain.task_id}
            </span>
            <span className="text-[10px] text-[var(--ink-35)] whitespace-nowrap">
              {chain.hops.length} step{chain.hops.length === 1 ? "" : "s"} · {fmtWhen(chain.updated_at)}
            </span>
          </div>
          <ShortlinkChainSteps chain={chain} />
        </div>
      ))}
    </div>
  );
}

const KIND_ICON: Record<TaskTimelineEvent["kind"], string> = {
  stage: ic.chevronRight,
  captcha: ic.shield,
  shortlink: ic.link,
  provider: ic.download,
  state: ic.alertTriangle,
  log: ic.alertTriangle,
};

export function TaskTimelineView({ timeline }: { timeline: TaskTimeline }) {
  const origin = timeline.started_at ?? timeline.events[0]?.at ?? 0;
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap text-[10.5px]">
        <SummaryChip label="Total time" value={fmtDuration(timeline.duration_seconds)} />
        <SummaryChip
          label="Captchas"
          value={timeline.captchas.total ? `${timeline.captchas.solved}/${timeline.captchas.total} solved` : "none"}
          tone={timeline.captchas.failed ? "warning" : timeline.captchas.total ? "success" : "neutral"}
        />
        <SummaryChip label="Shortlink steps" value={String(timeline.shortlink_hops)} />
        {timeline.stalled_at && <SummaryChip label="Stalled at" value={timeline.stalled_at} tone="warning" />}
        {timeline.error && <SummaryChip label="Error" value={timeline.error} tone="error" />}
      </div>
      {timeline.events.length === 0 ? (
        <p className="text-[11px] text-[var(--ink-35)]">No activity was recorded for this download.</p>
      ) : (
        <ol className="relative ml-2 border-l border-[var(--line-10)] space-y-1.5">
          {timeline.events.map((event, index) => (
            <li key={`${event.kind}-${index}`} className="relative pl-4">
              <span
                className="absolute -left-[5px] top-1.5 w-2.5 h-2.5 rounded-full border-2 border-[#15181d]"
                style={{ background: TONE_COLOR[event.tone] }}
              />
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-[9.5px] font-mono text-[var(--ink-30)] w-12 shrink-0 text-right" title={fmtClock(event.at)}>
                  +{fmtDuration(Math.max(0, event.at - origin))}
                </span>
                {event.captcha ? (
                  <CaptchaVendorLogo vendor={event.captcha.vendor} size={14} />
                ) : (
                  <Icon d={KIND_ICON[event.kind]} size={11} style={{ color: TONE_COLOR[event.tone] }} />
                )}
                <span
                  className="text-[11px] truncate"
                  style={{ color: event.tone === "neutral" ? "rgba(255,255,255,0.75)" : TONE_COLOR[event.tone] }}
                  title={event.title}
                >
                  {event.title}
                </span>
                {event.stalled && (
                  <span className="px-1 rounded text-[9px] font-bold uppercase bg-amber-500/20 text-amber-300">stalled</span>
                )}
                {event.duration_seconds !== undefined && event.duration_seconds !== null && (
                  <span className="text-[10px] font-mono text-[var(--ink-35)] whitespace-nowrap">{fmtDuration(event.duration_seconds)}</span>
                )}
                {event.captcha && (
                  <>
                    <span className="text-[9.5px] font-mono text-[var(--ink-25)] select-all" title={event.captcha.id}>
                      {event.captcha.id.slice(0, 8)}
                    </span>
                    <InspectButton url={event.captcha.page_url} label="Open the page that showed this captcha" compact />
                  </>
                )}
              </div>
              {event.detail && event.kind !== "stage" && event.detail !== event.title && (
                <div className="ml-14 text-[10px] text-[var(--ink-35)] truncate" title={event.detail}>{event.detail}</div>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function SummaryChip({ label, value, tone = "neutral" }: { label: string; value: string; tone?: TimelineTone }) {
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-white/[0.04] border border-[var(--line-05)] max-w-[320px]"
      title={`${label}: ${value}`}
    >
      <span className="text-[var(--ink-35)]">{label}</span>
      <span className="font-medium truncate" style={{ color: tone === "neutral" ? "rgba(255,255,255,0.8)" : TONE_COLOR[tone] }}>
        {value}
      </span>
    </span>
  );
}
