// Sign in to a VPN account by account number (Mullvad): Transfer makes its
// own WireGuard key, adds it to the account as a device, and every city
// becomes a location. No config files to download.
import React, { useState } from "react";
import { Icon, ic } from "../../icons";

export function AccountSignIn({ providerName, locations, onSignIn, onRefresh, onSignOut }: {
  providerName: string;
  /** Locations already added from the account; 0 when not signed in. */
  locations: number;
  onSignIn: (accountNumber: string) => Promise<void>;
  onRefresh: () => Promise<void>;
  onSignOut: () => Promise<void>;
}) {
  const [number, setNumber] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const digits = number.replace(/\D/g, "");

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try { await action(); setNumber(""); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  if (locations > 0) {
    return (
      <div className="cx-account">
        <p className="ui-note"><Icon d={ic.shield} size={13} />
          <span>Signed in · {locations} cities. The tunnel runs inside Transfer; the key never leaves this computer except as its public half.</span></p>
        <div className="cx-side-row">
          <button type="button" className="ui-btn" disabled={busy} onClick={() => void run(onRefresh)}>
            <Icon d={ic.refreshCw} size={12} /> Update server list
          </button>
          <button type="button" className="ui-btn" disabled={busy} onClick={() => void run(onSignOut)}
            title={`Removes these locations and frees the device slot on your ${providerName} account`}>
            <Icon d={ic.power} size={12} /> Sign out
          </button>
        </div>
        <div role="status" aria-live="polite" className={`ui-verdict${error ? " bad" : ""}`}>{error}</div>
      </div>
    );
  }
  return (
    <div className="cx-account">
      <label className="ui-label" htmlFor="cx-account-number">Account number</label>
      <div className="ui-inline">
        <input id="cx-account-number" className="ui-input mono" inputMode="numeric" autoComplete="off" spellCheck={false}
          value={number} onChange={(e) => setNumber(e.target.value)} placeholder="0000 0000 0000 0000"
          onKeyDown={(e) => { if (e.key === "Enter" && digits.length === 16) void run(() => onSignIn(digits)); }} />
        <button type="button" className="btn-accent ui-primary" disabled={digits.length !== 16 || busy}
          onClick={() => void run(() => onSignIn(digits))}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </div>
      <p className="cx-drop-help">
        Transfer creates its own key and adds it to your account as a device ({providerName} allows five). Every city
        becomes a location; switching between them needs nothing else.
      </p>
      <div role="status" aria-live="polite" className={`ui-verdict${error ? " bad" : ""}`}>{error}</div>
    </div>
  );
}
