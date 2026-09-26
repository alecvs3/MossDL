// Shortlinks: unravel one by hand to see every hop, and the chains recent
// downloads took on their way to the file host.
import React, { useEffect, useState } from "react";
import { getShortlinkHistory, resolveShortlinkChain, type ShortlinkHistoryChain, type ShortlinkResolutionResult } from "../../../api";
import { emitLog } from "../../../lib/telemetry";
import { ShortlinkHistoryList } from "../../components/activity";
import { Icon, ic } from "../../icons";
import { copyText } from "../../ui/copyText";

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function ShortlinksTab({ onAddUrl }: { onAddUrl?: (url: string) => void }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ShortlinkResolutionResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<ShortlinkHistoryChain[]>([]);

  useEffect(() => {
    getShortlinkHistory(30).then(setHistory, (e: unknown) => {
      void emitLog("WARNING", "ui:captchas", "Shortlink history failed to load", {}, e);
    });
  }, [result]);

  const unravel = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try { setResult(await resolveShortlinkChain(url.trim())); }
    catch (err) { setError(message(err)); }
    finally { setBusy(false); }
  };

  return (
    <div className="cp-body">
      <section className="cp-section">
        <h3>Unravel a shortlink</h3>
        <p className="cp-sub">See every hop a shortlink takes before it reaches the file host.</p>
        <form className="ui-inline" onSubmit={unravel}>
          <input className="ui-input" value={url} onChange={(e) => setUrl(e.target.value)} aria-label="Shortlink"
            placeholder="ouo.io, adshrink.it, tinyurl, rekonise…" />
          <button type="submit" className="btn-accent ui-primary" disabled={busy || !url.trim()}>
            <Icon d={ic.zap} size={12} /> {busy ? "Unravelling…" : "Unravel"}
          </button>
        </form>
        {error && <div role="alert" className="cp-error" style={{ marginTop: 10 }}><Icon d={ic.alertTriangle} size={13} />{error}</div>}
        {result && (
          <div className="cp-chain">
            <div className="cp-chain-final">
              <div className="grow">
                <div className="ui-chip ok">Reached after {result.hops.length} {result.hops.length === 1 ? "hop" : "hops"}</div>
                <div className="u" title={result.final_url}>{result.final_url}</div>
              </div>
              <button type="button" className="ui-btn sm" onClick={() => copyText(result.final_url, "link")}><Icon d={ic.copy} size={11} /> Copy</button>
              {onAddUrl && <button type="button" className="btn-accent ui-primary" onClick={() => onAddUrl(result.final_url)}><Icon d={ic.plus} size={11} /> Download</button>}
            </div>
            {result.hops.map((hop) => (
              <div key={hop.hop_number} className="cp-hop">
                <span className="cp-hop-n">{hop.hop_number}</span>
                <div style={{ minWidth: 0 }}>
                  <div className="cp-item-sub">
                    <span className="ui-chip">{hop.strategy.replace(/_/g, " ")}</span>
                    {hop.delay_seconds ? ` · waited ${hop.delay_seconds}s` : ""}{hop.captcha_encountered ? " · captcha" : ""}
                  </div>
                  <div className="u" title={hop.input_url}>{hop.input_url}</div>
                  <div className="u out" title={hop.output_url}>→ {hop.output_url}</div>
                  {hop.error && <div className="err">{hop.error}</div>}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
      <section className="cp-section">
        <h3>Recent chains</h3>
        <p className="cp-sub">The hops recent downloads took to reach their file host.</p>
        <ShortlinkHistoryList chains={history} />
      </section>
    </div>
  );
}
