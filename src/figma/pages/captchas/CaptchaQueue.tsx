// Captchas waiting for the user: what asked, how long is left, and every way
// to answer it (type it, let the solver try, open it in the browser, skip).
import React, { useState } from "react";
import type { CaptchaChallenge, CaptchaVendor } from "../../../api";
import { openCaptchaInBrowser, openPath } from "../../../api";
import { CaptchaVendorLogo } from "../../components/activity";
import { Icon, ic } from "../../icons";

export function captchaVendorFor(captchaType: string): CaptchaVendor {
  const value = captchaType.toLowerCase();
  if (value === "turnstile" || value === "browser_session") return "cloudflare";
  if (value.startsWith("recaptcha")) return "google";
  if (value.startsWith("hcaptcha")) return "hcaptcha";
  return "image";
}

// The engine's lifecycle states, in words; the engine says what they are, the UI only names them.
const STATE_TEXT: Record<string, string> = {
  detected: "Just asked", parked: "Waiting for a solver", routing: "Choosing a solver", solving: "Being solved",
  manual_required: "Needs you", verifying: "Checking the answer with the site", rejected: "The site rejected the answer",
};

const message = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback);

export function CaptchaQueue({ pending, onSolve, onSkip }: {
  pending: CaptchaChallenge[];
  onSolve?: (id: string, solution: Record<string, unknown>, generation?: number) => Promise<void>;
  onSkip?: (id: string, generation?: number) => Promise<void>;
}) {
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (id: string, action: () => Promise<void>, fallback: string) => {
    setBusyId(id);
    setError(null);
    try { await action(); }
    catch (e) { setError(message(e, fallback)); }
    finally { setBusyId(null); }
  };

  const submit = (c: CaptchaChallenge) => {
    const value = inputs[c.id]?.trim();
    if (!value || !onSolve) return;
    const isUrl = /^https?:\/\//i.test(value);
    const solution = c.captcha_type === "image_text" ? { text: value } : isUrl ? { link: value, token: value, direct: value } : { token: value };
    void run(c.id, async () => {
      await onSolve(c.id, solution, c.lifecycle?.generation);
      setInputs(({ [c.id]: _done, ...rest }) => rest);
    }, "Couldn't submit the answer");
  };

  if (!pending.length) {
    return (
      <div className="ui-empty-table">
        <Icon d={ic.shield} size={22} />
        <p>Nothing is waiting for you.</p>
        <p>When a host asks for a captcha the solvers can't answer, it appears here.</p>
      </div>
    );
  }
  return (
    <>
      {error && (
        <div role="alert" className="cp-error">
          <Icon d={ic.alertTriangle} size={13} /><span className="grow">{error}</span>
          <button type="button" className="ui-btn ghost sm" onClick={() => setError(null)} aria-label="Dismiss">Dismiss</button>
        </div>
      )}
      {pending.map((c) => {
        const type = String(c.captcha_type || "");
        const isImage = type === "image_text";
        const busy = busyId === c.id || c.lifecycle?.next_action === "wait";
        const image = c.params?.image_data
          ? (c.params.image_data.startsWith("data:") ? c.params.image_data : `data:image/png;base64,${c.params.image_data}`)
          : c.params?.image_url;
        return (
          <article key={c.id} className="cp-item" aria-label={`${c.provider_id || "Site"} captcha`}>
            <div className="cp-item-head">
              <span className="cp-vendor"><CaptchaVendorLogo vendor={captchaVendorFor(type)} size={18} /></span>
              <div className="grow">
                <div className="cp-item-title">{c.provider_id || "A site"} wants a captcha <span className="ui-chip">{type || "unknown"}</span></div>
                <div className="cp-item-sub" title={c.id}>
                  {c.lifecycle ? `${STATE_TEXT[c.lifecycle.state] ?? c.lifecycle.state}: ${c.lifecycle.reason}` : c.task_id ? `Download ${c.task_id.slice(0, 8)}` : "Not tied to a download"}
                  {c.lifecycle && c.lifecycle.generation > 1 ? ` · attempt ${c.lifecycle.generation}` : ""}
                </div>
              </div>
              <span className="cp-timer">Time left<b>{Math.max(0, Math.round(c.time_remaining || 0))}s</b></span>
            </div>
            {isImage && image && (
              <div className="cp-image"><img src={image} alt="Captcha to read" /><span>Type the characters shown</span></div>
            )}
            <div className="cp-item-actions">
              <input className="ui-input" value={inputs[c.id] ?? ""} aria-label="Answer"
                placeholder={isImage ? "Characters in the image" : "Paste the token or the direct download link"}
                onChange={(e) => setInputs((all) => ({ ...all, [c.id]: e.target.value }))}
                onKeyDown={(e) => { if (e.key === "Enter") submit(c); }} />
              <button type="button" className="btn-accent ui-primary" disabled={busy || !inputs[c.id]?.trim()} onClick={() => submit(c)}>
                {busy ? "Sending…" : "Submit"}
              </button>
              {onSolve && (
                <button type="button" className="ui-btn" disabled={busy} title="Hand it to the Clearcote solver"
                  onClick={() => void run(c.id, () => onSolve(c.id, {}, c.lifecycle?.generation), "Couldn't start the solver")}>
                  <Icon d={ic.zap} size={12} /> Solve automatically
                </button>
              )}
              <button type="button" className="ui-btn" disabled={busyId === c.id}
                title="Opens the site's page in your browser; the MossDL extension brings the answer back"
                onClick={() => void run(c.id, () => openCaptchaInBrowser(c.id, c.lifecycle?.generation), "Couldn't open it in the browser")}>
                <Icon d={ic.externalLink} size={12} /> Solve in browser
              </button>
              {c.loopback_url && (
                <button type="button" className="ui-btn ghost" title="A local helper page that shows just the captcha" onClick={() => void openPath(c.loopback_url!)}>
                  Helper page
                </button>
              )}
              {onSkip && (
                <button type="button" className="ui-btn ghost" disabled={busy}
                  onClick={() => void run(c.id, () => onSkip(c.id, c.lifecycle?.generation), "Couldn't skip")}>Skip</button>
              )}
            </div>
          </article>
        );
      })}
    </>
  );
}
