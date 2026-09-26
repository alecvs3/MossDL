// Setup guide for a provider (the app's existing written guides) plus the
// import box. Configs are validated by the engine's parser before saving.
import React, { useRef, useState } from "react";
import { importWireguardConfig } from "../../../api";
import { Icon, ic } from "../../icons";
import { MODE_TEXT, type Provider } from "./providers";
import { AccountSignIn } from "./AccountSignIn";
import { ProviderLogo } from "./ProviderLogo";

/** A location name from a file name such as "mullvad-se-sto-wg-001.conf" or "SE-12.conf". */
const locationFromFile = (name: string) => name.replace(/\.conf$/i, "").replace(/[_]+/g, " ").trim();

export function ProviderSetup({ provider, custom, onImport, onBack, account }: {
  provider?: Provider;
  custom: boolean;
  onImport: (conf: string, location: string) => Promise<void>;
  onBack?: () => void;
  /** Providers that sign in with an account number instead of config files. */
  account?: Omit<React.ComponentProps<typeof AccountSignIn>, "providerName">;
}) {
  const [conf, setConf] = useState("");
  const [location, setLocation] = useState("");
  const [check, setCheck] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [over, setOver] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const name = provider?.name ?? "Other WireGuard";
  const wireguard = !provider || ["any", "configs", "single"].includes(provider.mode);

  // Ask the engine's parser as the user types, so the verdict matches import
  // exactly. Only the newest request may update the verdict.
  const latest = useRef(0);
  const timer = useRef<number | undefined>(undefined);
  const validate = (text: string) => {
    setConf(text);
    window.clearTimeout(timer.current);
    const id = ++latest.current;
    if (!text.trim()) { setCheck(null); return; }
    timer.current = window.setTimeout(async () => {
      try {
        const parsed = await importWireguardConfig(text);
        if (id !== latest.current) return;
        setCheck({ ok: true, text: `Looks good · ${parsed.endpoint}` });
        setLocation((current) => current || parsed.endpoint.replace(/:\d+$/, ""));
      } catch (e) {
        if (id === latest.current) setCheck({ ok: false, text: e instanceof Error ? e.message : String(e) });
      }
    }, 250);
  };

  const importFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true);
    try {
      for (const file of [...files]) await onImport(await file.text(), locationFromFile(file.name));
    } catch (e) {
      setCheck({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const links = provider ? [
    provider.guideUrl && { url: provider.guideUrl, label: `${provider.name} setup page` },
    provider.openSourceGuideUrl && { url: provider.openSourceGuideUrl, label: "Open-source guide (gluetun wiki)" },
    provider.githubToolUrl && { url: provider.githubToolUrl, label: provider.githubToolLabel ?? "Community tool" },
  ].filter(Boolean) as { url: string; label: string }[] : [];

  return (
    <div className="cx-setup">
      <div className="cx-detail-top">
        <ProviderLogo provider={provider} kind={custom ? "custom" : undefined} size={44} />
        <div className="cx-detail-title">
          <h2>{name}</h2>
          <div className="ui-chips">
            {provider && <span className="ui-chip">{provider.badge}</span>}
            {provider && <span className={`ui-chip${provider.mode === "any" ? " ok" : ""}`}>{MODE_TEXT[provider.mode]}</span>}
          </div>
        </div>
        {onBack && <button type="button" className="ui-btn" onClick={onBack}>Back to locations</button>}
      </div>

      <div className="cx-guide page-scroll">
        <section aria-labelledby="cx-steps">
          <h3 id="cx-steps">Set up</h3>
          {provider?.setupSteps?.length ? (
            <ol className="cx-steps">{provider.setupSteps.map((step) => <li key={step}>{step}</li>)}</ol>
          ) : (
            <ol className="cx-steps">
              <li>Download a WireGuard configuration (.conf) from your provider's website.</li>
              <li>Drop it below, or paste its contents. Each file becomes one location.</li>
            </ol>
          )}
          {provider?.configNotes && <p className="ui-note"><Icon d={ic.shield} size={13} /><span>{provider.configNotes}</span></p>}
          {links.length > 0 && (
            <div className="ui-links">
              {links.map((l) => (
                <a key={l.url} href={l.url} target="_blank" rel="noreferrer noopener" className="ui-link">
                  {l.label} <Icon d={ic.externalLink} size={11} />
                </a>
              ))}
            </div>
          )}
        </section>

        <section aria-labelledby="cx-add">
          <h3 id="cx-add">Add {name}</h3>
          {account && <AccountSignIn providerName={name} {...account} />}
          {account && account.locations === 0 && <p className="ui-label">…or import config files</p>}
          {account && account.locations > 0 ? null : !wireguard ? (
            <p className="ui-note">
              <Icon d={ic.info} size={13} />
              <span>
                {provider?.mode === "tor" && "Tor runs as a local SOCKS5 proxy. Start Tor, then add socks5://127.0.0.1:9050 on the Proxies tab."}
                {provider?.mode === "adapter" && "ExpressVPN doesn't hand out WireGuard keys. Connect in the ExpressVPN app; Transfer then detects its network adapter as a System VPN connection."}
                {provider?.mode === "exit" && "Pick an exit node in Tailscale; Transfer routes through it like any system VPN."}
              </span>
            </p>
          ) : (
            <>
              <div
                className={`cx-drop${over ? " over" : ""}`}
                role="button"
                tabIndex={0}
                aria-describedby="cx-drop-help"
                onClick={() => fileRef.current?.click()}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileRef.current?.click(); } }}
                onDragOver={(e) => { e.preventDefault(); setOver(true); }}
                onDragLeave={() => setOver(false)}
                onDrop={(e) => { e.preventDefault(); setOver(false); void importFiles(e.dataTransfer.files); }}
              >
                <Icon d={ic.upload} size={20} />
                <div><b>{busy ? "Importing…" : "Drop .conf files here"}</b> or <span className="ui-link">browse</span></div>
                <div id="cx-drop-help" className="cx-drop-help">Several at once is fine — each becomes a location named after its file.</div>
                <input ref={fileRef} type="file" accept=".conf,text/plain" multiple hidden onChange={(e) => void importFiles(e.target.files)} />
              </div>
              <label className="ui-label" htmlFor="cx-conf">…or paste one</label>
              <textarea
                id="cx-conf"
                className="cx-textarea"
                spellCheck={false}
                value={conf}
                onChange={(e) => validate(e.target.value)}
                placeholder={"[Interface]\nPrivateKey = …\n\n[Peer]\nEndpoint = …"}
              />
              <div className="ui-inline">
                <input className="ui-input" value={location} onChange={(e) => setLocation(e.target.value)} placeholder="Location name, e.g. Stockholm" aria-label="Location name" />
                <button type="button" className="btn-accent ui-primary" disabled={!check?.ok || !location.trim() || busy}
                  onClick={async () => { setBusy(true); try { await onImport(conf, location.trim()); setConf(""); setLocation(""); setCheck(null); } finally { setBusy(false); } }}>
                  Import
                </button>
              </div>
              <div role="status" aria-live="polite" className={`ui-verdict${check ? (check.ok ? " ok" : " bad") : ""}`}>{check?.text}</div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
