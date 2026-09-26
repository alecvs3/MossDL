import { useEffect, useMemo, useState } from "react";
import type { CaptureCandidate, MediaPlan } from "../api";

export type MediaCaptureOverlayProps = {
  candidate: CaptureCandidate | null;
  open: boolean;
  onClose: () => void;
  onPlan: (url: string) => Promise<MediaPlan>;
  onImport: (candidate: CaptureCandidate, url: string) => Promise<Record<string, unknown>>;
};

function variantUrl(variant: Record<string, unknown> | null | undefined): string | null {
  const value = variant?.url ?? variant?.manifest_url;
  return typeof value === "string" && value ? value : null;
}

export function isEligibleMediaCandidate(candidate: CaptureCandidate | null): boolean {
  if (!candidate) return false;
  const mime = String(candidate.mime ?? "").toLowerCase();
  const path = candidate.url.split("?", 1)[0].toLowerCase();
  return mime.startsWith("audio/") || mime.startsWith("video/") || /\.(m3u8|mpd|mp4|webm|m4a|mp3|aac|ts|m4s)$/.test(path);
}

export default function MediaCaptureOverlay({ candidate, open, onClose, onPlan, onImport }: MediaCaptureOverlayProps) {
  const [plan, setPlan] = useState<MediaPlan | null>(null);
  const [selectedUrl, setSelectedUrl] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open || !candidate || !isEligibleMediaCandidate(candidate)) return;
    let cancelled = false;
    setPlan(null); setError(null); setBusy(true);
    void onPlan(candidate.url).then((next) => {
      if (cancelled) return;
      setPlan(next);
      setSelectedUrl(variantUrl(next.selected_variant) ?? candidate.url);
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "The media manifest could not be inspected");
    }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [candidate, onPlan, open]);

  const variants = useMemo(() => plan?.variants ?? [], [plan]);
  if (!open || !candidate) return null;
  const unsupported = Boolean(plan?.encrypted) || Boolean(error && /drm|encrypt|unsupported/i.test(error));

  return <div className="fixed inset-0 z-30 flex items-start justify-end p-5 bg-black/20" role="presentation" onClick={onClose}>
    <aside className="w-[min(440px,calc(100vw-40px))] max-h-[calc(100vh-40px)] overflow-auto rounded-lg border border-[var(--border-strong)] bg-[var(--surface-strong)] shadow-2xl p-4" role="dialog" aria-modal="true" aria-label="Media capture options" onClick={(event) => event.stopPropagation()}>
      <div className="flex items-start justify-between gap-3"><div><p className="eyebrow">MEDIA CAPTURE</p><h3 className="font-semibold">Choose a media variant</h3><p className="text-xs text-[var(--text-muted)] truncate max-w-[340px]">{candidate.filename || candidate.url}</p></div><button className="text-[var(--text-muted)]" onClick={onClose} aria-label="Close media options">×</button></div>
      {busy && <p className="text-xs text-[var(--text-muted)] mt-4">Inspecting media capabilities…</p>}
      {unsupported && <div className="mt-4 rounded-md border border-[var(--warning)] px-3 py-2 text-xs text-[var(--warning)]">Encrypted/DRM playback is unsupported or requires user action. No bypass will be attempted.</div>}
      {error && !unsupported && <div className="mt-4 rounded-md border border-[var(--danger)] px-3 py-2 text-xs text-[var(--danger)]">{error}</div>}
      {!busy && !unsupported && <div className="mt-4 space-y-2">
        <p className="text-xs text-[var(--text-muted)]">Recommended</p>
        <label className="flex items-center gap-2 rounded-md border border-[var(--accent-line)] bg-[var(--accent-soft)] px-3 py-2 text-sm"><input type="radio" name="media-variant" checked={selectedUrl === candidate.url || selectedUrl === variantUrl(plan?.selected_variant)} onChange={() => setSelectedUrl(variantUrl(plan?.selected_variant) ?? candidate.url)} />{candidate.filename || "Source media"}<span className="ml-auto text-xs text-[var(--text-muted)]">recommended</span></label>
        {variants.filter((variant) => variantUrl(variant) && variantUrl(variant) !== selectedUrl).map((variant, index) => { const url = variantUrl(variant)!; return <label key={`${url}-${index}`} className="flex items-center gap-2 rounded-md border border-[var(--border)] px-3 py-2 text-sm"><input type="radio" name="media-variant" checked={selectedUrl === url} onChange={() => setSelectedUrl(url)} />{String(variant.RESOLUTION ?? variant.resolution ?? variant.height ? `${variant.RESOLUTION ?? variant.resolution ?? `${variant.height}p`}` : `Alternative ${index + 1}`)}<span className="ml-auto text-xs text-[var(--text-muted)]">{String(variant.BANDWIDTH ?? variant.bandwidth ?? "")}</span></label>; })}
        <button className="btn-accent w-full rounded-md px-3 py-2 text-xs mt-3" disabled={!selectedUrl || busy} onClick={() => { setBusy(true); void onImport(candidate, selectedUrl).finally(() => setBusy(false)); }}>Add selected media to Downloads</button>
        <p className="text-[11px] text-[var(--text-subtle)]">The selected variant is handed to the engine as a normal durable task; the engine owns media assembly and progress.</p>
      </div>}
    </aside>
  </div>;
}
