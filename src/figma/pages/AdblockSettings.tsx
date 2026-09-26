// Ad blocking settings: the switch, whether to fetch uBlock Origin's full
// lists, and what the engine actually has on disk.
import React, { useCallback, useEffect, useState } from "react";
import { getAdblockStatus, updateFilterLists, type AdblockStatus } from "../../api";
import { formatBytes } from "../../lib/format";
import { emitLog } from "../../lib/telemetry";
import { SectionLabel, SettingRow, Toggle } from "../ui/SettingsControls";
import { toast } from "../ui/Toasts";

const age = (seconds: number | null) => {
  if (!seconds) return "never";
  const days = Math.floor((Date.now() / 1000 - seconds) / 86400);
  return days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
};

export function AdblockSettings({ on, fullLists, onToggle }: {
  on: boolean;
  fullLists: boolean;
  onToggle: (key: "adblock" | "adblockFullLists") => void;
}) {
  const [status, setStatus] = useState<AdblockStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try { setStatus(await getAdblockStatus()); }
    catch (e) { void emitLog("WARNING", "ui:settings", "Filter list status failed", {}, e); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const update = async () => {
    setBusy(true);
    try {
      const results = await updateFilterLists();
      const failed = Object.entries(results).filter(([, r]) => !r.ok);
      toast(failed.length ? `Couldn't update ${failed.map(([n]) => n).join(", ")}` : "Filter lists updated",
        { tone: failed.length ? "danger" : "ok" });
    } catch (e) {
      toast(`Update failed: ${e instanceof Error ? e.message : String(e)}`, { tone: "danger" });
    } finally {
      setBusy(false);
      void load();
    }
  };

  const listBytes = (status?.lists ?? []).reduce((sum, l) => sum + (l.bytes ?? 0), 0);
  const newest = Math.max(0, ...(status?.lists ?? []).map((l) => l.updated_at ?? 0));
  return (
    <>
      <SectionLabel title="Ad blocking" />
      <SettingRow label="Block ads and trackers" sub="Page crawls, capture review and the browser skip ads, using uBlock Origin's lists">
        <Toggle on={on} onToggle={() => onToggle("adblock")} />
      </SettingRow>
      {on && (
        <>
          <SettingRow label="Full filter lists" sub="EasyList, EasyPrivacy, uBlock filters and Peter Lowe's list, about 4 MB, refreshed weekly" indent>
            <Toggle on={fullLists} onToggle={() => onToggle("adblockFullLists")} />
          </SettingRow>
          <SettingRow indent label="Lists on this computer"
            sub={status?.builtin_only ? "Built-in ad domains only" : `${formatBytes(listBytes)} lists · ${formatBytes(status?.cache_bytes ?? 0)} compiled · updated ${age(newest)}`}>
            <button type="button" className="btn-accent px-3 py-1 rounded text-xs font-medium" disabled={busy || !fullLists} onClick={() => void update()}>
              {busy ? "Updating…" : "Update now"}
            </button>
          </SettingRow>
        </>
      )}
    </>
  );
}
