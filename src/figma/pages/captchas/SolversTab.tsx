// The engine's solver cascade, in the order it tries them, with what can be
// set for each: on/off, API keys (saved to the engine, shown masked) and
// balances; plus the Clearcote browser and a FlareSolverr endpoint.
import React, { useCallback, useEffect, useState } from "react";
import {
  checkCaptchaBalance, checkClearcoteStatus, checkFlareSolverrHealth, configureFlareSolverr, getCaptchaConfig,
  saveCaptchaConfig, uninstallClearcote, type CaptchaSolverConfig, type ClearcoteStatus,
} from "../../../api";
import { emitLog } from "../../../lib/telemetry";
import { Icon, ic } from "../../icons";
import { toast } from "../../ui/Toasts";
import { useClearcoteInstall } from "../../ui/useClearcoteInstall";

const KEYED = new Set(["twocaptcha", "anticaptcha", "capmonster", "audio_speech"]);
const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

function SolverRow({ solver, rank, onChanged }: { solver: CaptchaSolverConfig; rank: number; onChanged: () => void }) {
  const [key, setKey] = useState("");
  const [balance, setBalance] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async (patch: Partial<CaptchaSolverConfig>, done: string) => {
    setBusy(true);
    try { await saveCaptchaConfig({ solvers: [{ id: solver.id, ...patch }] }); toast(done, { tone: "ok" }); setKey(""); onChanged(); }
    catch (e) { toast(`Couldn't save ${solver.name}: ${message(e)}`, { tone: "danger" }); }
    finally { setBusy(false); }
  };
  const checkBalance = async () => {
    setBalance("Checking…");
    try { setBalance(`$${(await checkCaptchaBalance(solver.id)).balance.toFixed(2)}`); }
    catch (e) { setBalance(`Couldn't check: ${message(e)}`); }
  };
  return (
    <div className="cp-solver">
      <span className="cp-rank" title="Tried in this order">{rank}</span>
      <div style={{ minWidth: 0 }}>
        <div className="n">{solver.name}</div>
        {KEYED.has(solver.id) && (
          <>
            <div className="d">{solver.has_key ? `Key saved (${solver.masked_key})` : "No key saved"}{balance ? ` · balance ${balance}` : ""}</div>
            <div className="ui-inline">
              <input className="ui-input mono" type="password" value={key} onChange={(e) => setKey(e.target.value)}
                placeholder={solver.has_key ? "Replace the key" : "API key"} aria-label={`${solver.name} API key`} autoComplete="off" />
              <button type="button" className="ui-btn" disabled={!key.trim() || busy} onClick={() => void save({ api_key: key.trim() }, "Key saved")}>Save key</button>
              {solver.has_key && solver.id !== "audio_speech" && <button type="button" className="ui-btn ghost" onClick={() => void checkBalance()}>Balance</button>}
            </div>
          </>
        )}
      </div>
      <button type="button" role="switch" aria-checked={solver.enabled} aria-label={`Use ${solver.name}`} disabled={busy}
        className={`mini-switch${solver.enabled ? " on" : ""}`}
        onClick={() => void save({ enabled: !solver.enabled }, `${solver.name} ${solver.enabled ? "off" : "on"}`)} />
    </div>
  );
}

export function SolversTab() {
  const [solvers, setSolvers] = useState<CaptchaSolverConfig[] | null>(null);
  const [clearcote, setClearcote] = useState<ClearcoteStatus | null>(null);
  const [installing, setInstalling] = useState(false);
  const [flare, setFlare] = useState<{ available: boolean; endpoint: string } | null>(null);
  const [endpoint, setEndpoint] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setSolvers([...(await getCaptchaConfig()).solvers].sort((a, b) => a.priority - b.priority)); }
    catch (e) { setError(`Couldn't read the solver settings: ${message(e)}`); }
  }, []);
  useEffect(() => {
    void load();
    checkClearcoteStatus().then(setClearcote, (e: unknown) => void emitLog("WARNING", "ui:captchas", "Clearcote status failed", {}, e));
    // A network probe with its own timeout, off the page-switch path.
    checkFlareSolverrHealth().then((r) => { setFlare(r); setEndpoint(r.endpoint); },
      (e: unknown) => void emitLog("WARNING", "ui:captchas", "FlareSolverr probe failed", {}, e));
  }, [load]);

  const { install, start: startInstall } = useClearcoteInstall((done) => { if (done.status) setClearcote(done.status); });
  const uninstall = async () => {
    setInstalling(true);
    try { await uninstallClearcote(); setClearcote(await checkClearcoteStatus()); }
    catch (e) { toast(`Clearcote: ${message(e)}`, { tone: "danger" }); }
    finally { setInstalling(false); }
  };
  const saveEndpoint = async () => {
    try { setFlare(await configureFlareSolverr(endpoint.trim())); }
    catch (e) { toast(`FlareSolverr: ${message(e)}`, { tone: "danger" }); }
  };

  return (
    <div className="cp-body">
      <section className="cp-section">
        <h3>Solvers</h3>
        <p className="cp-sub">Tried top to bottom until one answers; the last ones ask you.</p>
        {error && <div role="alert" className="cp-error">{error}</div>}
        {solvers && (
          <div className="cp-solvers">
            {solvers.map((s, i) => <SolverRow key={s.id} solver={s} rank={i + 1} onChanged={() => void load()} />)}
          </div>
        )}
      </section>

      <section className="cp-section">
        <h3>Browser solver</h3>
        <div className="cp-card">
          <div className="cp-card-head">
            <span className={`ui-dot ${clearcote === null ? "busy" : clearcote.installed ? "ok" : ""}`} aria-hidden="true" />
            <div className="grow">
              <div className="cp-item-title">Clearcote</div>
              <div className="cp-item-sub">
                {install.running ? `${install.percent}% · ${install.message}`
                  : install.error ? `Install failed: ${install.error}`
                  : clearcote === null ? "Checking…" : clearcote.installed ? `Installed · version ${clearcote.version ?? "?"} · ${clearcote.size_mb} MB`
                  : "Not installed: challenges fall back to Camoufox or Chrome if present (about 230 MB download)"}
              </div>
            </div>
            {clearcote && (clearcote.installed
              ? <button type="button" className="ui-btn ghost" disabled={installing} onClick={() => void uninstall()}>Uninstall</button>
              : <button type="button" className="btn-accent ui-primary" disabled={install.running} onClick={() => void startInstall()}>{install.running ? "Installing…" : "Install"}</button>)}
          </div>
        </div>
        <div className="cp-card">
          <div className="cp-card-head">
            <span className={`ui-dot ${flare === null ? "busy" : flare.available ? "ok" : ""}`} aria-hidden="true" />
            <div className="grow">
              <div className="cp-item-title">FlareSolverr</div>
              <div className="cp-item-sub">{flare === null ? "Checking…" : flare.available ? "Reachable" : "Not running at this address"}</div>
            </div>
          </div>
          <div className="ui-inline">
            <input className="ui-input mono" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="http://127.0.0.1:8191" aria-label="FlareSolverr address" />
            <button type="button" className="ui-btn" disabled={!endpoint.trim()} onClick={() => void saveEndpoint()}><Icon d={ic.refreshCw} size={11} /> Save &amp; test</button>
          </div>
        </div>
      </section>
    </div>
  );
}
