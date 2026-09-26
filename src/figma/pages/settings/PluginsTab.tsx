import { useContext, useMemo, useState } from "react";
import { openPath, setPluginEnabled } from "../../../api";
import { Icon, ic, ProviderIcon, ProviderPresentationContext } from "../../icons";
import { Toggle } from "../../ui/SettingsControls";
import "./plugins.css";

// Where "Request a plugin" goes: the repo's plugin-request issue form.
export const PLUGIN_REQUEST_URL = "https://github.com/alecvs3/MossDL/issues/new?template=plugin_request.yml";

const CATEGORIES = [
  { id: "all", label: "All" },
  { id: "cloud", label: "Cloud storage" },
  { id: "file-host", label: "File hosts" },
  { id: "video", label: "Video" },
  { id: "images", label: "Images" },
  { id: "audio", label: "Audio" },
  { id: "social", label: "Social & forums" },
  { id: "any-site", label: "Any website" },
] as const;

// Manifest capabilities worth telling a user about, in plain words.
const FEATURES: Record<string, string> = {
  folders: "Folders",
  ranges: "Multi-connection",
  accounts: "Accounts",
  encrypted: "Encrypted",
  passwords: "Passwords",
  browser: "Browser",
};

/** "mega.nz", "bunkr.cr +7", or "Any site" for pattern-only plugins like the direct-link resolver. */
function hostSummary(hosts: string[]): string {
  const named = hosts.filter((h) => !h.includes("*"));
  if (named.length === 0) return hosts.some((h) => h !== "*") ? "Sites running this software" : "Any site";
  return named.length > 1 ? `${named[0]} +${named.length - 1}` : named[0];
}

export function PluginsTab() {
  const providers = useContext(ProviderPresentationContext);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<(typeof CATEGORIES)[number]["id"]>("all");
  // The engine's answer to a toggle, shown until the next snapshot carries it.
  const [answered, setAnswered] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return providers
      .filter((p) => category === "all" || p.category === category)
      .filter((p) => !q || p.display_name.toLowerCase().includes(q) || p.hosts.some((h) => h.includes(q)))
      .sort((a, b) => a.display_name.localeCompare(b.display_name));
  }, [providers, query, category]);

  const toggle = async (id: string, enabled: boolean) => {
    setError(null);
    try {
      const result = await setPluginEnabled(id, enabled);
      setAnswered((prev) => ({ ...prev, [id]: result.enabled ?? enabled }));
    } catch (err) {
      setError(`Couldn't change ${id}: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  return (
    <div className="pl-page">
      <header className="pl-head">
        <div>
          <h2>Plugins</h2>
          <p>{providers.length} sites MossDL knows how to download from. Turn one off to treat its links as plain web pages.</p>
        </div>
        <label className="pl-search">
          <Icon d={ic.search} size={12} />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search sites" aria-label="Search plugins" />
        </label>
      </header>

      <div className="pl-cats" role="tablist" aria-label="Plugin category">
        {CATEGORIES.map((c) => (
          <button key={c.id} type="button" role="tab" aria-selected={category === c.id}
            className={`pl-cat ${category === c.id ? "on" : ""}`} onClick={() => setCategory(c.id)}>
            {c.label}
          </button>
        ))}
      </div>

      {error && <p className="pl-error" role="alert">{error}</p>}

      <div className="pl-grid">
        {visible.map((p) => {
          const enabled = answered[p.id] ?? p.enabled;
          const features = (p.capabilities ?? []).filter((c) => FEATURES[c]);
          return (
            <article key={p.id} className={`pl-card ${enabled ? "" : "off"}`}>
              <div className="pl-tile"><ProviderIcon provider={p.id} size={24} /></div>
              <div className="pl-body">
                <div className="pl-name">{p.display_name}</div>
                <div className="pl-hosts" title={p.hosts.join(", ")}>{hostSummary(p.hosts)}</div>
                {features.length > 0 && (
                  <div className="pl-chips">{features.map((c) => <span key={c}>{FEATURES[c]}</span>)}</div>
                )}
              </div>
              <Toggle on={enabled} onToggle={() => void toggle(p.id, !enabled)} />
            </article>
          );
        })}
        <button type="button" className="pl-card pl-request" onClick={() => void openPath(PLUGIN_REQUEST_URL)
          .catch((err: unknown) => setError(`Couldn't open GitHub: ${String(err)}`))}>
          <div className="pl-tile"><Icon d={ic.plus} size={18} /></div>
          <div className="pl-body">
            <div className="pl-name">Request a plugin</div>
            <div className="pl-hosts">Missing a site? Ask for it on GitHub</div>
          </div>
          <Icon d={ic.externalLink} size={12} />
        </button>
      </div>
      {visible.length === 0 && query.trim() && <p className="pl-empty">No plugin matches “{query}”.</p>}
    </div>
  );
}
