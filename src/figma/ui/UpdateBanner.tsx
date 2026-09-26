import { useEffect, useState } from "react";
import { checkForUpdate, installUpdate, type UpdateCheck } from "../../api";

export function UpdateBanner() {
  const [update, setUpdate] = useState<UpdateCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    if ("__TAURI_INTERNALS__" in window) {
      void checkForUpdate().then(result => {
        if (!cancelled) setUpdate(result);
      }).catch(error => console.warn("[Updater] Automatic check failed", error));
    }
    return () => { cancelled = true; };
  }, []);
  if (!update?.configured || !update.available) return null;
  const install = async () => {
    setBusy(true);
    setError(null);
    try { await installUpdate(); }
    catch (error) { setError(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };
  return <div role="status" className="relative flex items-center gap-3 px-4 py-2 text-sm border-b border-white/10 bg-emerald-950 text-white">
    <span className="flex-1">MossDL {update.version} is available.{error && <span role="alert"> Update failed: {error}</span>}</span>
    <button className="btn-accent rounded px-3 py-1" disabled={busy} onClick={() => void install()}>{busy ? "Installing…" : "Install update"}</button>
    <button disabled={busy} onClick={() => setUpdate(null)} aria-label="Dismiss update notification">Later</button>
  </div>;
}
