// Captchas: what is waiting for you, what happened before, shortlinks, and the
// solvers that answer them. Everything shown comes from the engine.
import React, { useEffect, useState } from "react";
import { getUiSettings, updateUiSettings, type CaptchaChallenge } from "../../api";
import { emitLog } from "../../lib/telemetry";
import { PageTabs, type PageTab } from "../ui/PageTabs";
import { toast } from "../ui/Toasts";
import { CaptchaHistoryTab } from "./captchas/CaptchaHistoryTab";
import { CaptchaQueue } from "./captchas/CaptchaQueue";
import { ShortlinksTab } from "./captchas/ShortlinksTab";
import { SolversTab } from "./captchas/SolversTab";
import "./captchas/captchas.css";

type Tab = "waiting" | "history" | "shortlinks" | "solvers";

interface CaptchasPageProps {
  captchaPending?: CaptchaChallenge[];
  onCaptchaSolve?: (id: string, solution: Record<string, unknown>, generation?: number) => Promise<void>;
  onCaptchaSkip?: (id: string, generation?: number) => Promise<void>;
  onAddUrl?: (url: string) => void;
  autoSolve?: boolean;
  onAutoSolveChange?: (enabled: boolean) => void;
}

export function CaptchasPage({ captchaPending = [], onCaptchaSolve, onCaptchaSkip, onAddUrl, autoSolve = false, onAutoSolveChange }: CaptchasPageProps) {
  const [tab, setTab] = useState<Tab>(captchaPending.length ? "waiting" : "history");
  const [autoOpen, setAutoOpen] = useState<boolean | null>(null);
  const pendingKey = captchaPending.map((c) => `${c.id}:${c.status}`).join(",");

  useEffect(() => {
    getUiSettings().then(
      (s) => setAutoOpen(typeof s?.captcha?.captchaAutoOpenManual === "boolean" ? s.captcha.captchaAutoOpenManual : true),
      (e: unknown) => void emitLog("WARNING", "ui:captchas", "Could not read captcha settings", {}, e),
    );
  }, []);

  const toggleAutoOpen = async () => {
    const next = !autoOpen;
    setAutoOpen(next);
    try { await updateUiSettings({ captcha: { captchaAutoOpenManual: next } }); }
    catch (e) {
      setAutoOpen(!next);
      toast(`Couldn't save: ${e instanceof Error ? e.message : String(e)}`, { tone: "danger" });
    }
  };

  const tabs: PageTab<Tab>[] = [
    { id: "waiting", label: "Waiting", count: captchaPending.length, alert: captchaPending.length > 0 },
    { id: "history", label: "History" },
    { id: "shortlinks", label: "Shortlinks" },
    { id: "solvers", label: "Solvers" },
  ];

  return (
    <div className="page-shell">
      <div className="page-head">
        <PageTabs label="Captchas" idPrefix="cp" value={tab} onChange={setTab} tabs={tabs} />
        <span className="grow" />
        <button type="button" role="switch" aria-checked={autoSolve} className="head-switch" disabled={!onAutoSolveChange}
          title="Hand new captchas to the solvers without asking" onClick={() => onAutoSolveChange?.(!autoSolve)}>
          Solve automatically <span className={`mini-switch${autoSolve ? " on" : ""}`} aria-hidden="true" />
        </button>
        <button type="button" role="switch" aria-checked={!!autoOpen} className="head-switch" disabled={autoOpen === null}
          title="Open a captcha that needs you in your own browser" onClick={() => void toggleAutoOpen()}>
          Open in browser <span className={`mini-switch${autoOpen ? " on" : ""}`} aria-hidden="true" />
        </button>
      </div>

      <section id="cp-panel-waiting" role="tabpanel" aria-labelledby="cp-tab-waiting" hidden={tab !== "waiting"} className="page-panel">
        {tab === "waiting" && <div className="cp-body"><CaptchaQueue pending={captchaPending} onSolve={onCaptchaSolve} onSkip={onCaptchaSkip} /></div>}
      </section>
      <section id="cp-panel-history" role="tabpanel" aria-labelledby="cp-tab-history" hidden={tab !== "history"} className="page-panel">
        {tab === "history" && <CaptchaHistoryTab refreshKey={pendingKey} />}
      </section>
      <section id="cp-panel-shortlinks" role="tabpanel" aria-labelledby="cp-tab-shortlinks" hidden={tab !== "shortlinks"} className="page-panel">
        {tab === "shortlinks" && <ShortlinksTab onAddUrl={onAddUrl} />}
      </section>
      <section id="cp-panel-solvers" role="tabpanel" aria-labelledby="cp-tab-solvers" hidden={tab !== "solvers"} className="page-panel">
        {tab === "solvers" && <SolversTab />}
      </section>
    </div>
  );
}
export default CaptchasPage;
