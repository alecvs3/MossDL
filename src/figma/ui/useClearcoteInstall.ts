// The Clearcote download as the engine reports it: start it, then read its
// real progress while it runs. Used by the welcome wizard and Captchas > Solvers.
import { useCallback, useEffect, useRef, useState } from "react";
import { getClearcoteInstall, startClearcoteInstall, type ClearcoteInstall } from "../../api";
import { emitLog } from "../../lib/telemetry";

const IDLE: ClearcoteInstall = { running: false, percent: 0, message: "", error: null, status: null };

export function useClearcoteInstall(onInstalled?: (install: ClearcoteInstall) => void) {
  const [install, setInstall] = useState<ClearcoteInstall>(IDLE);
  const done = useRef(onInstalled);
  done.current = onInstalled;

  // Pick up an install already running (e.g. started from the other screen).
  useEffect(() => {
    getClearcoteInstall().then((state) => { if (state.running) setInstall(state); },
      (e: unknown) => void emitLog("WARNING", "ui:clearcote", "Install state unavailable", {}, e));
  }, []);

  useEffect(() => {
    if (!install.running) return;
    // The engine updates progress per downloaded megabyte; half a second is smooth enough.
    const timer = window.setInterval(() => {
      getClearcoteInstall().then((state) => {
        setInstall(state);
        if (!state.running && state.status) done.current?.(state);
      }, (e: unknown) => {
        void emitLog("WARNING", "ui:clearcote", "Install progress unavailable", {}, e);
        setInstall((s) => ({ ...s, running: false, error: e instanceof Error ? e.message : String(e) }));
      });
    }, 500);
    return () => window.clearInterval(timer);
  }, [install.running]);

  const start = useCallback(async () => {
    try { setInstall(await startClearcoteInstall()); }
    catch (e) { setInstall({ ...IDLE, error: e instanceof Error ? e.message : String(e) }); }
  }, []);

  return { install, start };
}
