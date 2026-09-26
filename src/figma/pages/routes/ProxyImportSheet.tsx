// Side panel for pasting proxies. Shows exactly what will be imported and
// why any line was skipped before anything is saved.
import React, { useEffect, useMemo, useRef, useState } from "react";
import { fetchPublicProxies } from "../../../api";
import { Icon, ic } from "../../icons";
import { toast } from "../../ui/Toasts";
import { parseProxyList, type ParsedProxy, type ProxyKind } from "./proxyParse";

export function ProxyImportSheet({ onClose, onImport }: { onClose: () => void; onImport: (list: ParsedProxy[]) => Promise<void> }) {
  const [text, setText] = useState("");
  const [fallback, setFallback] = useState<ProxyKind>("http_proxy");
  const [busy, setBusy] = useState(false);
  const areaRef = useRef<HTMLTextAreaElement | null>(null);
  const returnFocus = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null);
  const parsed = useMemo(() => parseProxyList(text, fallback), [text, fallback]);

  useEffect(() => {
    areaRef.current?.focus();
    const target = returnFocus.current;
    return () => { if (target?.isConnected) target.focus(); };
  }, []);

  const loadPublic = async () => {
    setBusy(true);
    try {
      const list = await fetchPublicProxies(40);
      setText((t) => [t.trim(), ...list.map((p) => `${p.protocols.includes("socks5") ? "socks5" : p.protocols.includes("http") || p.protocols.includes("https") ? "http" : p.protocols[0] ?? "http"}://${p.ip}:${p.port}`)].filter(Boolean).join("\n"));
    } catch (e) {
      toast(`Couldn't load the public list: ${e instanceof Error ? e.message : String(e)}`, { tone: "danger" });
    } finally { setBusy(false); }
  };

  const submit = async () => {
    if (!parsed.proxies.length) return;
    setBusy(true);
    try { await onImport(parsed.proxies); }
    catch (e) { toast(`Import failed: ${e instanceof Error ? e.message : String(e)}`, { tone: "danger" }); }
    finally { setBusy(false); }
  };

  return (
    <aside className="cx-sheet" role="dialog" aria-labelledby="cx-sheet-h"
      onKeyDown={(e) => { if (e.key === "Escape") onClose(); if (e.key === "Enter" && e.ctrlKey) void submit(); }}>
      <div className="cx-sheet-head">
        <h3 id="cx-sheet-h">Add proxies</h3>
        <button type="button" className="cx-icon" aria-label="Close" title="Close (Esc)" onClick={onClose}><Icon d={ic.close} size={12} /></button>
      </div>
      <p className="cx-fmt">
        One per line, formats can be mixed:<br />
        <code>host:port</code> · <code>host:port:user:pass</code> · <code>user:pass@host:port</code> · <code>socks5://user:pass@host:port</code> · JSON
      </p>
      <textarea ref={areaRef} className="cx-textarea tall" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)} aria-label="Proxies, one per line" />
      <label className="cx-sheet-row">
        Type when a line doesn't say
        <select className="cx-select" value={fallback} onChange={(e) => setFallback(e.target.value as ProxyKind)}>
          <option value="http_proxy">HTTP</option>
          <option value="socks5">SOCKS5</option>
        </select>
      </label>
      <div className="cx-parsed" aria-live="polite">
        <div className="cx-parsed-head">
          <span>{parsed.proxies.length} recognised</span>
          {parsed.rejected.length > 0 && <span className="bad">{parsed.rejected.length} skipped</span>}
        </div>
        {parsed.proxies.map((p) => (
          <div key={`${p.kind}${p.host}${p.port}`} className="cx-parsed-row">
            <span className="cx-type">{p.kind === "socks5" ? "SOCKS5" : "HTTP"}</span>
            <span className="mono truncate">{p.host}:{p.port}</span>
            {p.username && <span title="Login saved to the secret store" aria-label="with login"><Icon d={ic.key} size={10} /></span>}
          </div>
        ))}
        {parsed.rejected.map((r) => (
          <div key={r.line} className="cx-parsed-row bad" title={r.line}>
            <span className="mono truncate">{r.line}</span>
            <span className="cx-reason">{r.reason}</span>
          </div>
        ))}
      </div>
      <div className="cx-sheet-foot">
        <button type="button" className="ui-link" disabled={busy} onClick={() => void loadPublic()}>Add from a public list…</button>
        <button type="button" className="btn-accent ui-primary" disabled={busy || !parsed.proxies.length} onClick={() => void submit()} title="Ctrl+Enter">
          Import {parsed.proxies.length || ""}
        </button>
      </div>
    </aside>
  );
}
