// "Updates" in Settings > General: check the release feed and install, with
// every outcome stated (including builds that were not released with updates).
import React, { useState } from "react";
import { checkForUpdate, installUpdate, type UpdateCheck } from "../../api";
import { SectionLabel, SettingRow } from "../ui/SettingsControls";

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function UpdateSettings() {
  const [result, setResult] = useState<UpdateCheck | null>(null);
  const [busy, setBusy] = useState<"check" | "install" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastAction, setLastAction] = useState<"check" | "install">("check");

  const run = async (kind: "check" | "install") => {
    setBusy(kind);
    setLastAction(kind);
    setError(null);
    try {
      if (kind === "check") setResult(await checkForUpdate());
      else await installUpdate();
    } catch (e) { setError(message(e)); }
    finally { setBusy(null); }
  };

  const status = error ? `Couldn't ${lastAction === "install" ? "install" : "check"}: ${error}`
    : !result ? "Not checked yet"
    : !result.configured ? "This build was not released with updates; download new versions yourself"
    : result.available ? `Version ${result.version} is available (you have ${result.current})`
    : "You have the latest version";
  return (
    <>
      <SectionLabel title="Updates" />
      <SettingRow label="App updates" sub={status}>
        {result?.available ? (
          <button type="button" className="btn-accent px-3 py-1 rounded text-xs font-medium" disabled={busy !== null} onClick={() => void run("install")}>
            {busy === "install" ? "Installing…" : "Install and restart"}
          </button>
        ) : (
          <button type="button" className="btn-accent px-3 py-1 rounded text-xs font-medium" disabled={busy !== null} onClick={() => void run("check")}>
            {busy === "check" ? "Checking…" : "Check now"}
          </button>
        )}
      </SettingRow>
    </>
  );
}
