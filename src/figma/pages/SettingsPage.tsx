import React, { useState, useCallback, useEffect } from "react";
import type { AppSettings, SettingsTab } from "../types";
import type { RouteProfile, CaptchaChallenge, ProviderAccount } from "../../api";
import {
  listAccounts,
  createAccount,
  deleteAccount,
  getGoogleOAuthUrl,
  openPath,
} from "../../api";
import { Icon, ic } from "../icons";
import { Toggle, SettingRow, SectionLabel } from "../ui/SettingsControls";
import { AdblockSettings } from "./AdblockSettings";
import { UpdateSettings } from "./UpdateSettings";
import { ShortcutsSettings } from "./ShortcutsSettings";
import { parseHotkeyMap, type HotkeyMap } from "../../lib/hotkeys";

import { THEMES } from "../themes";
import { PluginsTab } from "./settings/PluginsTab";

export function SettingsPage({
  settings,
  onSettingChange,
  routes = [],
  captchaPending = [],
  onPickDirectory,
  onSaveSecret,
  onCaptchaSolve,
  onCaptchaSkip,
}: {
  settings: AppSettings;
  onSettingChange?: (tab: SettingsTab, key: string, value: string | boolean) => void;
  routes?: RouteProfile[];
  captchaPending?: CaptchaChallenge[];
  onPickDirectory?: () => Promise<string | null>;
  onSaveSecret?: (name: string, value: string) => Promise<void>;
  onCaptchaSolve?: (id: string, solution: Record<string, unknown>, generation?: number) => Promise<void>;
  onCaptchaSkip?: (id: string, generation?: number) => Promise<void>;
}) {
  // "plugins" is a view, not a settings section: nothing in it is a setting.
  const [tab, setTab] = useState<SettingsTab | "plugins">("general");
  const [captchaConfirm, setCaptchaConfirm] = useState(false);
  const [connectingTo, setConnectingTo] = useState<string | null>(null);
  const [captchaKey, setCaptchaKey] = useState("");
  const [captchaSolutions, setCaptchaSolutions] = useState<Record<string, string>>({});
  const [captchaBusy, setCaptchaBusy] = useState<string | null>(null);
  const [captchaError, setCaptchaError] = useState<string | null>(null);

  const [accountsList, setAccountsList] = useState<ProviderAccount[]>([]);
  const [loadingAccounts, setLoadingAccounts] = useState(false);
  const [showAddAccount, setShowAddAccount] = useState(false);
  const [newAccProvider, setNewAccProvider] = useState("google-drive");
  const [newAccLabel, setNewAccLabel] = useState("");
  const [newAccSecret, setNewAccSecret] = useState("");
  const [newAccType, setNewAccType] = useState<string>("oauth");
  const [googleAuthUrl, setGoogleAuthUrl] = useState<string | null>(null);
  const [accountError, setAccountError] = useState<string | null>(null);
  const [accountSuccess, setAccountSuccess] = useState<string | null>(null);

  const refreshAccounts = useCallback(async () => {
    setLoadingAccounts(true);
    try {
      const data = await listAccounts();
      setAccountsList(data);
    } catch {
      // Ignored
    } finally {
      setLoadingAccounts(false);
    }
  }, []);

  useEffect(() => {
    if (tab === "accounts") {
      void refreshAccounts();
      void getGoogleOAuthUrl().then((res) => setGoogleAuthUrl(res.auth_url)).catch(() => {});
    }
  }, [refreshAccounts, tab]);

  const handleCreateAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newAccLabel.trim() || !newAccSecret.trim()) return;
    setAccountError(null);
    setAccountSuccess(null);
    try {
      await createAccount({
        provider_id: newAccProvider,
        label: newAccLabel.trim(),
        account_type: newAccType,
        secret_value: newAccSecret.trim(),
      });
      setAccountSuccess(`Account '${newAccLabel}' saved successfully!`);
      setNewAccLabel("");
      setNewAccSecret("");
      setShowAddAccount(false);
      void refreshAccounts();
    } catch (err) {
      setAccountError(err instanceof Error ? err.message : "Failed to save account");
    }
  };

  const handleDeleteAccount = async (accountId: string) => {
    try {
      await deleteAccount(accountId);
      void refreshAccounts();
    } catch (err) {
      setAccountError(err instanceof Error ? err.message : "Failed to delete account");
    }
  };

  const solvePendingCaptcha = async (id: string, value: string, type: string) => {
    if (!onCaptchaSolve || !value.trim()) return;
    setCaptchaBusy(id);
    setCaptchaError(null);
    const solution = type === "image_text" ? { text: value.trim() } : { token: value.trim() };
    try {
      await onCaptchaSolve(id, solution);
    } catch (error) {
      setCaptchaError(error instanceof Error ? error.message : "Could not solve CAPTCHA");
    } finally {
      setCaptchaBusy(null);
    }
  };
  const skipPendingCaptcha = async (id: string) => {
    if (!onCaptchaSkip) return;
    setCaptchaBusy(id);
    setCaptchaError(null);
    try {
      await onCaptchaSkip(id);
    } catch (error) {
      setCaptchaError(error instanceof Error ? error.message : "Could not skip CAPTCHA");
    } finally {
      setCaptchaBusy(null);
    }
  };

  const tog = useCallback(
    (k: keyof AppSettings) => tab !== "plugins" && onSettingChange?.(tab, String(k), !settings[k]),
    [onSettingChange, settings, tab]
  );
  const set = useCallback(
    (k: keyof AppSettings, v: string | boolean) => tab !== "plugins" && onSettingChange?.(tab, String(k), v),
    [onSettingChange, tab]
  );

  const tabs: { id: SettingsTab | "plugins"; icon: string; label: string }[] = [
    { id: "general", icon: ic.settings, label: "General" },
    { id: "network", icon: ic.network, label: "Network" },
    { id: "appearance", icon: ic.eye, label: "Appearance" },
    { id: "notifications", icon: ic.bell, label: "Alerts" },
    { id: "captcha", icon: ic.shield, label: "CAPTCHA" },
    { id: "shortcuts", icon: ic.terminal, label: "Shortcuts" },
    { id: "routes", icon: ic.wifi, label: "Connections" },
    { id: "accounts", icon: ic.userCheck, label: "Accounts" },
    { id: "plugins", icon: ic.layers, label: "Plugins" },
  ];

  const switchLocation = (id: string) => {
    if (id === settings.activeLocation || connectingTo) return;
    setConnectingTo(id);
    set("activeLocation", id);
    setConnectingTo(null);
  };

  const routeLocations = routes.map((route) => ({
    id: route.id,
    flag: "↗",
    name: route.id,
    country: route.region || "",
    // The engine does not yet expose per-route ping/server/health metrics; do
    // not invent them. Unknown health renders as a neutral indicator.
    servers: 0,
    ping: 0,
    health: "unknown",
  }));
  const routeKinds: Record<string, string> = {
    Direct: "direct",
    SOCKS5: "socks5",
    "HTTP Proxy": "http_proxy",
    "System VPN": "system_vpn",
    WireGuard: "wireguard",
  };
  const activeLoc =
    routeLocations.find((l) => l.id === settings.activeLocation) ??
    routeLocations[0] ?? { id: "direct", flag: "↗", name: "Direct", country: "", servers: 0, ping: 0, health: "unknown" };

  return (
    <div className="flex h-full min-h-0">
      {/* subnav */}
      <div
        className="w-32 shrink-0 overflow-y-auto pt-1 px-2 space-y-0.5"
        style={{ borderRight: "1px solid var(--line-06)" }}
      >
        <p
          style={{
            fontSize: "12.5px",
            fontWeight: 700,
            color: "var(--ink-90)",
            padding: "10px 6px 12px",
          }}
        >
          Settings
        </p>
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className="w-full flex items-center gap-2 px-2.5 py-2 rounded-md transition-all text-left"
            style={{
              background: tab === t.id ? "var(--accent-soft)" : "transparent",
              color: tab === t.id ? "var(--accent-light)" : "var(--ink-60)",
              fontSize: "11px",
              fontWeight: 500,
              border: tab === t.id ? "1px solid var(--accent-line)" : "1px solid transparent",
            }}
            onMouseEnter={(e) => {
              if (tab !== t.id) e.currentTarget.style.background = "var(--surface-05)";
            }}
            onMouseLeave={(e) => {
              if (tab !== t.id) e.currentTarget.style.background = "transparent";
            }}
          >
            <Icon d={t.icon} size={12} />
            {t.label}
          </button>
        ))}
      </div>

      {/* content */}
      <div className="flex-1 min-h-0 overflow-y-auto px-5 pb-6">
        {tab === "plugins" && <PluginsTab />}

        {/* ── GENERAL ─────────────────────────────────────────── */}
        {tab === "general" && (
          <>
            <SectionLabel title="Startup" />
            <SettingRow label="Launch on startup" sub="Start MossDL when Windows boots">
              <Toggle on={settings.startWithWindows} onToggle={() => tog("startWithWindows")} />
            </SettingRow>
            <SettingRow label="Minimize to tray" sub="Keep running in tray when closed">
              <Toggle on={settings.systemTray} onToggle={() => tog("systemTray")} />
            </SettingRow>
            <SettingRow label="Resume on launch" sub="Auto-resume unfinished downloads">
              <Toggle on={settings.autoStart} onToggle={() => tog("autoStart")} />
            </SettingRow>
            <SettingRow label="Clipboard watcher" sub="Capture copied links into LinkGrabber">
              <Toggle on={settings.clipboardWatcher} onToggle={() => tog("clipboardWatcher")} />
            </SettingRow>
            <SettingRow label="Setup Wizard" sub="Launch the initial configuration wizard">
              <button
                type="button"
                className="px-2.5 py-1 rounded text-xs font-semibold text-white bg-blue-600 hover:bg-blue-500 transition-colors"
                onClick={() => {
                  try {
                    localStorage.removeItem("onboarding_dismissed");
                  } catch {}
                  onSettingChange?.("general", "onboardingCompleted", false);
                }}
              >
                Launch Wizard
              </button>
            </SettingRow>
            <SettingRow label="Browser Extension" sub="MossDL Capture for Chrome, Edge, and Firefox">
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className="px-2.5 py-1 rounded text-xs font-medium text-white bg-[var(--surface-08)] hover:bg-[var(--surface-12)] border border-[var(--line-09)] transition-colors flex items-center gap-1.5"
                  onClick={() => void openPath("https://chromewebstore.google.com/detail/mossdl-capture/dknlgnaknfaoflbmobdknjfjdagieccm")}
                >
                  Chrome Store <Icon d={ic.externalLink} size={11} />
                </button>
                <button
                  type="button"
                  className="px-2.5 py-1 rounded text-xs font-medium text-white bg-[var(--surface-08)] hover:bg-[var(--surface-12)] border border-[var(--line-09)] transition-colors flex items-center gap-1.5"
                  onClick={() => void openPath("https://addons.mozilla.org/firefox/addon/mossdl-capture/")}
                >
                  Firefox AMO <Icon d={ic.externalLink} size={11} />
                </button>
              </div>
            </SettingRow>
            <SectionLabel title="Storage" />
            <SettingRow label="Default save location" sub="Where files are saved by default">
              <div className="flex items-center gap-1.5">
                <input
                  type="text"
                  className="win-input"
                  value={settings.savePath}
                  onChange={(e) => set("savePath", e.target.value)}
                  style={{ width: "140px" }}
                />
                <button
                  type="button"
                  className="w-7 h-7 rounded-md flex items-center justify-center"
                  onClick={() => {
                    const pick = onPickDirectory?.();
                    if (pick)
                      void pick.then((path) => {
                        if (path) set("savePath", path);
                      });
                  }}
                  style={{
                    background: "var(--surface-08)",
                    color: "var(--ink-50)",
                    border: "1px solid var(--line-09)",
                  }}
                >
                  <Icon d={ic.folder} size={12} />
                </button>
              </div>
            </SettingRow>
            <SettingRow label="Clear history on exit" sub="Remove completed items on close">
              <Toggle on={settings.clearOnExit} onToggle={() => tog("clearOnExit")} />
            </SettingRow>
            <UpdateSettings />
            <SectionLabel title="Downloads" />
            <SettingRow label="Max concurrent downloads" sub="Downloads running simultaneously">
              <select
                className="win-select"
                value={settings.maxConcurrent}
                onChange={(e) => set("maxConcurrent", e.target.value)}
              >
                {["1", "2", "3", "4", "5", "8", "10"].map((n) => (
                  <option key={n}>{n}</option>
                ))}
              </select>
            </SettingRow>
            <SettingRow label="Retry failed downloads" sub="Auto-retry on connection error">
              <Toggle on={settings.retryFailed} onToggle={() => tog("retryFailed")} />
            </SettingRow>
            {settings.retryFailed && (
              <SettingRow label="Retry attempts" sub="Attempts before giving up" indent>
                <select
                  className="win-select"
                  value={settings.retryCount}
                  onChange={(e) => set("retryCount", e.target.value)}
                >
                  {["1", "2", "3", "5", "10"].map((n) => (
                    <option key={n}>{n}</option>
                  ))}
                </select>
              </SettingRow>
            )}
          </>
        )}

        {/* ── NETWORK ─────────────────────────────────────────── */}
        {tab === "network" && (
          <>
            <SectionLabel title="Speed" />
            <SettingRow label="Limit download speed" sub="Cap bandwidth used by MossDL">
              <Toggle on={settings.speedLimit} onToggle={() => tog("speedLimit")} />
            </SettingRow>
            {settings.speedLimit && (
              <SettingRow label="Speed cap" sub="Maximum download speed" indent>
                <div className="flex items-center gap-1.5">
                  <input
                    type="number"
                    className="win-input"
                    value={settings.speedLimitVal}
                    onChange={(e) => set("speedLimitVal", e.target.value)}
                    style={{ width: "60px", textAlign: "right" }}
                  />
                  <span style={{ fontSize: "10px", color: "var(--ink-35)" }}>MB/s</span>
                </div>
              </SettingRow>
            )}
            <SectionLabel title="Proxy" />
            <SettingRow label="Use proxy server" sub="Route downloads through a proxy">
              <Toggle on={settings.proxy} onToggle={() => tog("proxy")} />
            </SettingRow>
            {settings.proxy && (
              <SettingRow label="Proxy address" sub="Format: host:port" indent>
                <input
                  type="text"
                  className="win-input"
                  value={settings.proxyAddr}
                  onChange={(e) => set("proxyAddr", e.target.value)}
                  placeholder="127.0.0.1:8080"
                  style={{ width: "175px" }}
                />
              </SettingRow>
            )}
            <SectionLabel title="Connections" />
            <SettingRow label="Connections per file" sub="Parallel segments per download">
              <select
                className="win-select"
                value={settings.connectionsPerFile}
                onChange={(e) => set("connectionsPerFile", e.target.value)}
              >
                {["1", "2", "4", "8", "16", "32"].map((n) => (
                  <option key={n}>{n}</option>
                ))}
              </select>
            </SettingRow>
            <SettingRow label="Timeout" sub="Seconds before stalled connection drops">
              <select
                className="win-select"
                value={settings.timeoutSeconds}
                onChange={(e) => set("timeoutSeconds", e.target.value)}
              >
                {["10", "30", "60", "120"].map((n) => (
                  <option key={n}>{n}s</option>
                ))}
              </select>
            </SettingRow>
            <AdblockSettings on={settings.adblock} fullLists={settings.adblockFullLists} onToggle={tog} />
          </>
        )}

        {/* ── SHORTCUTS ───────────────────────────────────────── */}
        {tab === "shortcuts" && (
          <ShortcutsSettings
            map={parseHotkeyMap(settings.hotkeys)}
            onChange={(next: HotkeyMap) =>
              // Bindings live with the rest of the settings, under General.
              onSettingChange?.("general", "hotkeys", JSON.stringify(next))
            }
          />
        )}

        {/* ── APPEARANCE ──────────────────────────────────────── */}
        {tab === "appearance" && (
          <>
            <SectionLabel title="Theme" />
            <div className="theme-gallery">
              {THEMES.map((theme) => {
                const active = (settings.themeName || "midnight") === theme.id;
                return (
                  <button
                    key={theme.id}
                    type="button"
                    onClick={() => {
                      set("themeName", theme.id);
                      set("colorAccent", theme.accent);
                    }}
                    className={`theme-card${active ? " active" : ""}`}
                    aria-pressed={active}
                    title={theme.description}
                  >
                    <span className="theme-swatch" style={{ background: theme.preview.base }}>
                      <span style={{ background: theme.preview.surface }} />
                      <span style={{ background: theme.preview.accent }} />
                      <span style={{ background: theme.preview.success }} />
                      <span style={{ background: theme.preview.warning }} />
                    </span>
                    <span className="theme-name">{theme.name}</span>
                    <span className="theme-sub">{theme.description}</span>
                  </button>
                );
              })}
            </div>

            <SectionLabel title="Window" />
            <SettingRow label="Frosted glass window" sub="On: stronger Acrylic blur. Off: subtler Mica tint. Windows draws both.">
              <Toggle on={settings.acrylic} onToggle={() => tog("acrylic")} />
            </SettingRow>
            <SettingRow label="Row size" sub="Density of the download list">
              <div className="segmented">
                {(["small", "medium", "large"] as const).map((s) => (
                  <button
                    key={s}
                    className={`segmented-btn${settings.rowSize === s ? " active" : ""}`}
                    onClick={() => set("rowSize", s)}
                    style={{
                      color: settings.rowSize === s ? "rgba(255,255,255,0.9)" : undefined,
                      background: settings.rowSize === s ? "rgba(0,120,212,0.35)" : undefined,
                    }}
                  >
                    {s.charAt(0).toUpperCase() + s.slice(1)}
                  </button>
                ))}
              </div>
            </SettingRow>
            <SettingRow label="Show speed indicators" sub="Live speed next to active downloads">
              <Toggle on={settings.showSpeeds} onToggle={() => tog("showSpeeds")} />
            </SettingRow>
            <SectionLabel title="Display & Accessibility Scaling" />
            <SettingRow
              label="Interface scale"
              sub="Scales entire UI (menus, toolbars, fonts, dialogs) for optimal legibility"
            >
              <div className="segmented">
                {(["100%", "110%", "125%", "150%", "Auto"] as const).map((sc) => {
                  const active = (settings.uiScale || "Auto") === sc;
                  return (
                    <button
                      key={sc}
                      className={`segmented-btn${active ? " active" : ""}`}
                      onClick={() => set("uiScale", sc)}
                      style={{
                        color: active ? "rgba(255,255,255,0.9)" : undefined,
                        background: active ? "rgba(0,120,212,0.35)" : undefined,
                      }}
                    >
                      {sc}
                    </button>
                  );
                })}
              </div>
            </SettingRow>
            <SettingRow
              label="Auto-enlarge on Fullscreen / Ultrawide"
              sub="Dynamically enlarges interface elements and typography on wide monitors and maximized windows"
            >
              <Toggle
                on={settings.autoFullscreenScale ?? true}
                onToggle={() => tog("autoFullscreenScale")}
              />
            </SettingRow>
            <SectionLabel title="Theme" />
            <SettingRow label="Color mode">
              <select
                className="win-select"
                value={settings.colorMode}
                onChange={(e) => set("colorMode", e.target.value)}
              >
                <option>Dark</option>
                <option>Light</option>
                <option>System</option>
              </select>
            </SettingRow>
            <SettingRow label="Accent color" sub="Matches your Windows accent">
              <div className="flex items-center gap-1.5">
                {["var(--accent)", "#107C10", "#E81123", "#9333ea", "#ea580c"].map((c) => (
                  <button
                    key={c}
                    onClick={() => set("colorAccent", c)}
                    className="w-5 h-5 rounded-full transition-all"
                    style={{
                      background: c,
                      boxShadow: settings.colorAccent === c ? `0 0 0 2px #fff, 0 0 0 4px ${c}` : "none",
                    }}
                  />
                ))}
              </div>
            </SettingRow>
            <SectionLabel title="Language" />
            <SettingRow label="Display language">
              <select
                className="win-select"
                value={settings.language}
                onChange={(e) => set("language", e.target.value)}
              >
                <option>English (US)</option>
                <option>Deutsch</option>
                <option>Español</option>
                <option>Français</option>
                <option>日本語</option>
              </select>
            </SettingRow>
            <SectionLabel title="Typography" />
            <SettingRow label="Interface font" sub="Choose the typeface used throughout the application">
              <select
                className="win-select"
                value={settings.fontFamily || "Inter"}
                onChange={(e) => set("fontFamily", e.target.value)}
              >
                <option value="Inter">Inter (Default)</option>
                <option value="Segoe UI">Segoe UI (System)</option>
                <option value="Roboto">Roboto</option>
                <option value="JetBrains Mono">JetBrains Mono (Monospace)</option>
                <option value="system-ui">System Default</option>
              </select>
            </SettingRow>
          </>
        )}

        {/* ── NOTIFICATIONS ───────────────────────────────────── */}
        {tab === "notifications" && (
          <>
            <SectionLabel title="Alerts" />
            <SettingRow label="Download complete" sub="Notify when a file finishes">
              <Toggle on={settings.notifComplete} onToggle={() => tog("notifComplete")} />
            </SettingRow>
            <SettingRow label="Download failed" sub="Notify on error">
              <Toggle on={settings.notifFailed} onToggle={() => tog("notifFailed")} />
            </SettingRow>
            <SettingRow label="Download paused" sub="Notify on automatic pause">
              <Toggle on={settings.notifPause} onToggle={() => tog("notifPause")} />
            </SettingRow>
            <SectionLabel title="Sound" />
            <SettingRow label="Play sound on complete" sub="Audio cue when a download finishes">
              <Toggle on={settings.notifSound} onToggle={() => tog("notifSound")} />
            </SettingRow>
            {settings.notifSound && (
              <SettingRow label="Sound preset" indent>
                <select
                  className="win-select"
                  value={settings.soundPreset}
                  onChange={(e) => set("soundPreset", e.target.value)}
                >
                  <option>Windows Notify</option>
                  <option>Chime</option>
                  <option>Ding</option>
                  <option>Custom…</option>
                </select>
              </SettingRow>
            )}
          </>
        )}

        {/* ── CAPTCHA & SOLVERS ───────────────────────────────── */}
        {tab === "captcha" && (
          <>
            {/* consent warning */}
            <div className="captcha-warning mt-4">
              <div className="flex items-start gap-3">
                <Icon
                  d={ic.alertTriangle}
                  size={18}
                  style={{ color: "var(--warning)", flexShrink: 0, marginTop: "1px" }}
                />
                <div className="flex-1">
                  <p
                    style={{
                      fontSize: "11.5px",
                      fontWeight: 600,
                      color: "var(--warning)",
                      marginBottom: "4px",
                    }}
                  >
                    Third-party solver services may violate provider ToS
                  </p>
                  <p style={{ fontSize: "10.5px", color: "var(--ink-50)", lineHeight: 1.6 }}>
                    Automated CAPTCHA solving, especially via paid services, may breach terms of service for
                    some providers. Local solvers (audio, FlareSolverr) carry lower risk. Only enable what you
                    understand and accept.
                  </p>
                </div>
              </div>
              <div
                className="flex items-center justify-between mt-3 pt-3"
                style={{ borderTop: "1px solid rgba(245,158,11,0.2)" }}
              >
                <span style={{ fontSize: "10.5px", color: "var(--ink-60)" }}>
                  I understand and accept the risks
                </span>
                <Toggle
                  on={settings.captchaAcknowledged}
                  onToggle={() => {
                    tog("captchaAcknowledged");
                    if (settings.captchaMaster) set("captchaMaster", false);
                  }}
                />
              </div>
            </div>

            {/* master toggle + inline confirm */}
            <SectionLabel title="Solver Engine" />
            <SettingRow
              label="Enable automated CAPTCHA solving"
              sub="Master switch for all solver backends"
            >
              <Toggle
                on={settings.captchaMaster}
                onToggle={() => {
                  if (!settings.captchaAcknowledged) return;
                  if (!settings.captchaMaster) setCaptchaConfirm(true);
                  else set("captchaMaster", false);
                }}
                disabled={!settings.captchaAcknowledged}
              />
            </SettingRow>

            {captchaConfirm && !settings.captchaMaster && (
              <div className="captcha-danger mb-2">
                <div className="flex items-start gap-3">
                  <Icon d={ic.shield} size={16} style={{ color: "var(--danger)", flexShrink: 0 }} />
                  <div className="flex-1">
                    <p style={{ fontSize: "11px", fontWeight: 600, color: "#fca5a5", marginBottom: "3px" }}>
                      Enable automated CAPTCHA solving?
                    </p>
                    <p style={{ fontSize: "10px", color: "var(--ink-45)", lineHeight: 1.5 }}>
                      This will allow MossDL to automatically solve CAPTCHA challenges encountered during
                      downloads. Built-in solvers run locally. Third-party API solvers transmit challenge data
                      to external services.
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2 mt-3">
                  <button
                    onClick={() => {
                      set("captchaMaster", true);
                      setCaptchaConfirm(false);
                    }}
                    className="px-3 py-1.5 rounded-md text-white transition-colors"
                    style={{
                      fontSize: "11px",
                      fontWeight: 600,
                      background: "#dc2626",
                      border: "1px solid rgba(239,68,68,0.4)",
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = "#ef4444")}
                    onMouseLeave={(e) => (e.currentTarget.style.background = "#dc2626")}
                  >
                    I understand — enable solvers
                  </button>
                  <button
                    onClick={() => setCaptchaConfirm(false)}
                    style={{ fontSize: "11px", color: "var(--ink-45)", padding: "6px 10px" }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {/* sub-toggles */}
            <div
              style={{
                opacity: settings.captchaMaster ? 1 : 0.4,
                pointerEvents: settings.captchaMaster ? "auto" : "none",
                transition: "opacity 0.2s",
              }}
            >
              <SectionLabel title="Built-in Solvers" />
              <SettingRow
                label="Audio solver / speech-to-text"
                sub="Solves audio CAPTCHA challenges locally"
              >
                <Toggle on={settings.captchaAudio} onToggle={() => tog("captchaAudio")} />
              </SettingRow>
              <SettingRow label="hCaptcha support" sub="Handle hCaptcha challenges">
                <Toggle on={settings.captchaHcaptcha} onToggle={() => tog("captchaHcaptcha")} />
              </SettingRow>
              <SettingRow label="reCAPTCHA v2 / v3" sub="Handle Google reCAPTCHA challenges">
                <Toggle on={settings.captchaRecaptcha} onToggle={() => tog("captchaRecaptcha")} />
              </SettingRow>
              <SettingRow label="Image / positional click" sub="Off by default — experimental">
                <Toggle on={settings.captchaPositional} onToggle={() => tog("captchaPositional")} />
              </SettingRow>
              <SettingRow label="Auto-skip after timeout" sub="Skip unsolvable challenges after delay">
                <div className="flex items-center gap-2">
                  <Toggle on={settings.captchaAutoSkip} onToggle={() => tog("captchaAutoSkip")} />
                  {settings.captchaAutoSkip && (
                    <input
                      type="number"
                      className="win-input"
                      value={settings.captchaAutoSkipSecs}
                      onChange={(e) => set("captchaAutoSkipSecs", e.target.value)}
                      style={{ width: "52px", textAlign: "right" }}
                    />
                  )}
                  {settings.captchaAutoSkip && (
                    <span style={{ fontSize: "10px", color: "var(--ink-35)" }}>s</span>
                  )}
                </div>
              </SettingRow>
              <SettingRow label="CAPTCHA sound alert" sub="Play a sound when a challenge appears">
                <Toggle on={settings.captchaSound} onToggle={() => tog("captchaSound")} />
              </SettingRow>

              <SectionLabel title="External API Solvers" />
              <div className="captcha-warning mb-2" style={{ padding: "8px 12px" }}>
                <div className="flex items-center gap-2">
                  <Icon d={ic.alertTriangle} size={12} style={{ color: "var(--warning)" }} />
                  <span style={{ fontSize: "10px", color: "var(--warning)" }}>
                    External solvers transmit challenge data to third-party servers
                  </span>
                </div>
              </div>
              <SettingRow label="2captcha / Anti-Captcha" sub="Paid API service — off by default">
                <Toggle on={settings.captcha2captcha} onToggle={() => tog("captcha2captcha")} />
              </SettingRow>
              {settings.captcha2captcha && (
                <SettingRow label="API key" sub="Stored securely in system keychain" indent>
                  <div className="flex items-center gap-1.5">
                    <Icon d={ic.key} size={12} style={{ color: "var(--ink-30)" }} />
                    <input
                      type="password"
                      className="win-input"
                      placeholder="••••••••••••••••••••"
                      style={{ width: "150px" }}
                      value={captchaKey}
                      onChange={(e) => setCaptchaKey(e.target.value)}
                      onBlur={() => {
                        if (captchaKey.trim()) {
                          const save = onSaveSecret?.("captcha-2captcha", captchaKey.trim());
                          if (save) void save.then(() => setCaptchaKey(""));
                        }
                      }}
                    />
                  </div>
                </SettingRow>
              )}
              <SettingRow label="FlareSolverr" sub="Local Cloudflare bypass service — off by default">
                <Toggle on={settings.captchaFlare} onToggle={() => tog("captchaFlare")} />
              </SettingRow>
              {settings.captchaFlare && (
                <SettingRow label="FlareSolverr endpoint" indent>
                  <div className="flex items-center gap-1.5">
                    <input
                      type="text"
                      className="win-input"
                      value={settings.captchaFlareEndpoint}
                      onChange={(e) => set("captchaFlareEndpoint", e.target.value)}
                      placeholder="http://localhost:8191"
                      style={{ width: "180px" }}
                    />
                    <span
                      className="w-2 h-2 rounded-full shrink-0"
                      style={{
                        background: settings.captchaFlareEndpoint ? "var(--success)" : "rgba(255,255,255,0.2)",
                      }}
                    />
                  </div>
                </SettingRow>
              )}
              <SettingRow
                label="Auto-solve every CAPTCHA"
                sub="Send new challenges straight to the solver instead of waiting for a click on the banner"
              >
                <Toggle
                  on={settings.captchaAutoSolve ?? false}
                  onToggle={() => set("captchaAutoSolve", !(settings.captchaAutoSolve ?? false))}
                />
              </SettingRow>
              <SettingRow
                label="Auto-open Cloudflare Turnstile in browser"
                sub="Automatically launch helper in default browser when Cloudflare challenge is encountered"
              >
                <Toggle
                  on={settings.captchaAutoOpenManual ?? true}
                  onToggle={() => set("captchaAutoOpenManual", !(settings.captchaAutoOpenManual ?? true))}
                />
              </SettingRow>
              <SettingRow
                label="Show automatic CAPTCHA banner"
                sub="Keep the banner visible while Clearcote is solving automatically"
              >
                <Toggle
                  on={settings.captchaShowAutoBanner ?? true}
                  onToggle={() => set("captchaShowAutoBanner", !(settings.captchaShowAutoBanner ?? true))}
                />
              </SettingRow>
            </div>

            {/* pending queue */}
            <SectionLabel title="Pending Challenges" />
            {captchaError && (
              <p role="alert" style={{ fontSize: "10px", color: "var(--danger)", marginBottom: "8px" }}>
                {captchaError}
              </p>
            )}
            {captchaPending.length === 0 && (
              <p className="figma-empty-state" style={{ fontSize: "10px", padding: "12px 0" }}>
                No pending CAPTCHA challenges.
              </p>
            )}
            {captchaPending.map((ch) => {
              const solution = captchaSolutions[ch.id] || "";
              const busy = captchaBusy === ch.id;
              return (
                <div
                  key={ch.id}
                  className="flex items-center gap-3 px-3 py-2.5 rounded-lg mb-1"
                  style={{ background: "var(--surface-03)", border: "1px solid var(--line-07)" }}
                >
                  <Icon d={ic.shield} size={14} style={{ color: "var(--warning)", flexShrink: 0 }} />
                  <div className="flex-1 min-w-0">
                    <p style={{ fontSize: "11px", fontWeight: 500, color: "var(--ink-80)" }}>
                      {ch.provider_id}
                    </p>
                    <p style={{ fontSize: "9.5px", color: "var(--ink-35)" }}>
                      {ch.captcha_type} · {ch.task_id || "transfer"}
                    </p>
                  </div>
                  <span
                    style={{
                      fontSize: "9.5px",
                      color: "var(--warning)",
                      fontFamily: "'JetBrains Mono',monospace",
                    }}
                  >
                    {Math.ceil(ch.time_remaining)}s
                  </span>
                  <input
                    className="win-input"
                    value={solution}
                    disabled={busy}
                    placeholder={ch.captcha_type === "image_text" ? "Text" : "Token"}
                    aria-label={`Solution for ${ch.provider_id}`}
                    style={{ width: "110px" }}
                    onChange={(e) =>
                      setCaptchaSolutions((current) => ({ ...current, [ch.id]: e.target.value }))
                    }
                  />
                  <button
                    disabled={!solution.trim() || busy || !onCaptchaSolve}
                    onClick={() => void solvePendingCaptcha(ch.id, solution, ch.captcha_type)}
                    className="px-2 py-1 rounded text-white transition-colors"
                    style={{
                      fontSize: "10px",
                      fontWeight: 600,
                      background: "rgba(0,120,212,0.3)",
                      border: "1px solid rgba(77,166,245,0.3)",
                    }}
                  >
                    {busy ? "…" : "Solve"}
                  </button>
                  <button
                    disabled={busy || !onCaptchaSkip}
                    onClick={() => void skipPendingCaptcha(ch.id)}
                    style={{ fontSize: "10px", color: "var(--ink-30)" }}
                  >
                    Skip
                  </button>
                </div>
              );
            })}
          </>
        )}

        {/* ── VPN & ROUTES ────────────────────────────────────── */}
        {tab === "routes" && (
          <>
            {/* active route card */}
            <div className="route-card p-4 mt-4 mb-4">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  {connectingTo ? (
                    <Icon d={ic.refreshCw} size={14} className="spin" style={{ color: "var(--warning)" }} />
                  ) : (
                    <span
                      className="w-2 h-2 rounded-full"
                      style={{
                        background: settings.activeLocation === "" ? "var(--danger)" : "var(--success)",
                        boxShadow: `0 0 6px ${settings.activeLocation === "" ? "var(--danger)" : "var(--success)"}`,
                      }}
                    />
                  )}
                  <span style={{ fontSize: "11px", fontWeight: 600, color: "var(--ink-85)" }}>
                    {connectingTo ? "Connecting…" : activeLoc.name}
                  </span>
                </div>
                <span
                  style={{
                    fontSize: "9.5px",
                    color: "var(--ink-35)",
                    fontFamily: "'JetBrains Mono',monospace",
                  }}
                >
                    {/* No fabricated documentation IP: show the real endpoint host when known. */}
                    {(routes.find((r) => r.id === activeLoc.id)?.endpoint || "").replace(/^[^@/]*@/, "") || "—"}
                </span>
              </div>
              <div className="flex items-center gap-4">
                <div>
                  <p
                    style={{
                      fontSize: "8.5px",
                      color: "var(--ink-30)",
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                    }}
                  >
                    Latency
                  </p>
                  <p
                    style={{
                      fontSize: "13px",
                      fontWeight: 700,
                      color: "var(--success)",
                      fontFamily: "'JetBrains Mono',monospace",
                    }}
                  >
                    {activeLoc.ping}
                    <span style={{ fontSize: "9px", fontWeight: 400 }}>ms</span>
                  </p>
                </div>
                <div>
                  <p
                    style={{
                      fontSize: "8.5px",
                      color: "var(--ink-30)",
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                    }}
                  >
                    Route type
                  </p>
                  <p style={{ fontSize: "11px", fontWeight: 500, color: "var(--ink-70)" }}>
                    {settings.routeType}
                  </p>
                </div>
                <div>
                  <p
                    style={{
                      fontSize: "8.5px",
                      color: "var(--ink-30)",
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                    }}
                  >
                    Status
                  </p>
                  <p
                    style={{
                      fontSize: "11px",
                      fontWeight: 500,
                      color: connectingTo ? "var(--warning)" : "var(--success)",
                    }}
                  >
                    {connectingTo ? "Switching" : "Connected"}
                  </p>
                </div>
              </div>
            </div>

            {/* warning */}
            <div
              className="flex items-start gap-2 px-3 py-2.5 rounded-lg mb-4"
              style={{ background: "rgba(245,158,11,0.08)", border: "1px solid rgba(245,158,11,0.2)" }}
            >
              <Icon
                d={ic.alertTriangle}
                size={13}
                style={{ color: "var(--warning)", flexShrink: 0, marginTop: "1px" }}
              />
              <p style={{ fontSize: "10px", color: "var(--ink-45)", lineHeight: 1.6 }}>
                Rotating routes does not bypass authentication, quota, or provider-policy failures. Route
                switching applies to new connections only.
              </p>
            </div>

            {/* route type selector */}
            <SectionLabel title="Route Type" />
            <div className="segmented mb-4">
              {["Direct", "SOCKS5", "HTTP Proxy", "System VPN", "WireGuard"].map((r) => {
                const available =
                  r === "Direct" ||
                  routes.some((route) => route.enabled !== false && route.kind === routeKinds[r]);
                return (
                  <button
                    key={r}
                    disabled={!available}
                    onClick={() => set("routeType", r)}
                    className={`segmented-btn ${settings.routeType === r ? "active" : ""}`}
                    title={available ? undefined : "Configure a matching route profile first"}
                  >
                    {r}
                  </button>
                );
              })}
            </div>

            {/* locations */}
            {settings.routeType !== "Direct" && (
              <>
                <SectionLabel title="Server Location" />
                <div className="space-y-0.5 mb-4">
                  {routeLocations.map((loc) => {
                    const isActive = settings.activeLocation === loc.id;
                    const isConnecting = connectingTo === loc.id;
                    const healthColor =
                      loc.health === "good"
                        ? "var(--success)"
                        : loc.health === "ok"
                          ? "var(--warning)"
                          : loc.health === "unknown"
                            ? "rgba(255,255,255,0.25)"
                            : "var(--danger)";
                    return (
                      <div
                        key={loc.id}
                        onClick={() => !isActive && !connectingTo && switchLocation(loc.id)}
                        className={`route-location-row ${isActive ? "active" : ""}`}
                        style={{ cursor: isActive || connectingTo ? "default" : "pointer" }}
                      >
                        <span style={{ fontSize: "16px", lineHeight: 1, flexShrink: 0 }}>{loc.flag}</span>
                        <span
                          style={{
                            flex: 1,
                            fontSize: "11.5px",
                            fontWeight: isActive ? 600 : 400,
                            color: isActive ? "var(--accent-light)" : "rgba(255,255,255,0.78)",
                          }}
                        >
                          {loc.name}
                        </span>
                        {loc.servers > 0 && (
                          <span style={{ fontSize: "9px", color: "var(--ink-30)" }}>
                            {loc.servers} servers
                          </span>
                        )}
                        {isConnecting ? (
                          <Icon d={ic.refreshCw} size={12} className="spin" style={{ color: "var(--warning)" }} />
                        ) : isActive ? (
                          <Icon d={ic.check} size={12} style={{ color: "var(--accent-light)" }} />
                        ) : (
                          <div className="flex items-center gap-1.5">
                            <span
                              style={{
                                fontSize: "9.5px",
                                color: "var(--ink-35)",
                                fontFamily: "'JetBrains Mono',monospace",
                              }}
                            >
                              {loc.ping > 0 ? `${loc.ping}ms` : "—"}
                            </span>
                            <span className="w-1.5 h-1.5 rounded-full" style={{ background: healthColor }} />
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </>
            )}

            {/* route history */}
            <SectionLabel title="Recent Route History" />
            <div className="space-y-1">
              {([] as Array<{ time: string; route: string; result: string }>).map((r, i) => {
                const rc = r.result === "success" ? "var(--success)" : r.result === "rotated" ? "var(--warning)" : "var(--danger)";
                return (
                  <div
                    key={i}
                    className="flex items-center gap-3 px-3 py-1.5"
                    style={{ background: "var(--surface-02)", borderRadius: "6px" }}
                  >
                    <span
                      style={{
                        fontSize: "9.5px",
                        color: "var(--ink-30)",
                        fontFamily: "'JetBrains Mono',monospace",
                        width: "48px",
                      }}
                    >
                      {r.time}
                    </span>
                    <span
                      style={{
                        fontSize: "10px",
                        color: "var(--ink-60)",
                        fontFamily: "'JetBrains Mono',monospace",
                        flex: 1,
                      }}
                    >
                      {r.route}
                    </span>
                    <span style={{ fontSize: "9.5px", color: rc, fontWeight: 600, textTransform: "capitalize" }}>
                      {r.result}
                    </span>
                  </div>
                );
              })}
            </div>
          </>
        )}

        {tab === "accounts" && (
          <div className="space-y-6">
            <div className="flex items-center justify-between">
              <div>
                <p style={{ fontSize: "12px", fontWeight: 700, color: "var(--ink-90)" }}>
                  Provider & Hoster Accounts
                </p>
                <p style={{ fontSize: "10px", color: "var(--ink-40)", marginTop: "2px" }}>
                  Add accounts for Google Drive, Real-Debrid, Mega, and hosters to bypass anonymous bandwidth caps and quota limits.
                </p>
              </div>

              <button
                onClick={() => setShowAddAccount((v) => !v)}
                className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 rounded text-xs font-medium text-white transition-colors flex items-center gap-1.5"
              >
                <Icon d={ic.plus} size={12} />
                <span>{showAddAccount ? "Cancel" : "Add Account"}</span>
              </button>
            </div>

            {accountSuccess && (
              <div className="p-2.5 bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-xs rounded">
                {accountSuccess}
              </div>
            )}

            {accountError && (
              <div className="p-2.5 bg-red-500/10 border border-red-500/20 text-red-400 text-xs rounded">
                {accountError}
              </div>
            )}

            {/* Add Account Drawer / Form */}
            {showAddAccount && (
              <form
                onSubmit={handleCreateAccount}
                className="p-4 rounded-xl bg-white/[0.03] border border-[var(--line-10)] space-y-3.5"
              >
                <p style={{ fontSize: "11px", fontWeight: 600, color: "var(--ink-85)" }}>
                  Configure New Hoster Account
                </p>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label className="text-[10px] text-[var(--ink-50)] block mb-1">Provider / Service</label>
                    <select
                      value={newAccProvider}
                      onChange={(e) => setNewAccProvider(e.target.value)}
                      className="w-full bg-black/40 border border-[var(--line-10)] rounded px-2.5 py-1.5 text-xs text-white"
                    >
                      <option value="google-drive">Google Drive (OAuth & Cookies)</option>
                      <option value="mega">Mega</option>
                      <option value="real-debrid">Real-Debrid</option>
                      <option value="1fichier">1Fichier</option>
                      <option value="rapidgator">Rapidgator</option>
                      <option value="mediafire">MediaFire</option>
                      <option value="generic">Generic Hoster</option>
                    </select>
                  </div>

                  <div>
                    <label className="text-[10px] text-[var(--ink-50)] block mb-1">Account Label / Name</label>
                    <input
                      type="text"
                      value={newAccLabel}
                      onChange={(e) => setNewAccLabel(e.target.value)}
                      placeholder="e.g. My Google Account"
                      className="w-full bg-black/40 border border-[var(--line-10)] rounded px-2.5 py-1.5 text-xs text-white placeholder:text-[var(--ink-30)]"
                      required
                    />
                  </div>
                </div>

                {newAccProvider === "google-drive" && (
                  <div className="p-3 bg-blue-500/10 border border-blue-500/20 rounded-lg space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="text-[11px] font-semibold text-blue-300">
                        Google Drive Authorization
                      </span>
                      {googleAuthUrl && (
                        <button
                          type="button"
                          onClick={() => openPath(googleAuthUrl)}
                          className="text-[10px] text-blue-400 hover:underline flex items-center gap-1"
                        >
                          <Icon d={ic.externalLink} size={11} />
                          <span>Authorize with Google in Browser</span>
                        </button>
                      )}
                    </div>
                    <p className="text-[10px] text-[var(--ink-50)]">
                      Paste an OAuth access token, Google authorization code, or browser session cookie (<code>SID=...</code>).
                    </p>
                  </div>
                )}

                <div>
                  <label className="text-[10px] text-[var(--ink-50)] block mb-1">
                    {newAccProvider === "google-drive" ? "OAuth Token or Session Cookie" : "API Key or Credentials"}
                  </label>
                  <textarea
                    value={newAccSecret}
                    onChange={(e) => setNewAccSecret(e.target.value)}
                    rows={3}
                    placeholder={
                      newAccProvider === "google-drive"
                        ? "Paste ya29.oauth_token... or Cookie: SID=... HSID=..."
                        : "Enter API key or password..."
                    }
                    className="w-full bg-black/40 border border-[var(--line-10)] rounded p-2.5 text-xs font-mono text-white placeholder:text-[var(--ink-30)]"
                    required
                  />
                </div>

                <div className="flex justify-end gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => setShowAddAccount(false)}
                    className="px-3 py-1.5 text-xs text-[var(--ink-50)] hover:text-white"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded text-xs font-medium"
                  >
                    Save Account
                  </button>
                </div>
              </form>
            )}

            {/* List of Configured Accounts */}
            <div className="space-y-2">
              <SectionLabel title={`Configured Accounts (${accountsList.length})`} />

              {accountsList.length === 0 ? (
                <div className="p-8 rounded-xl bg-white/[0.015] border border-[var(--line-05)] text-center text-[11.5px] text-[var(--ink-40)]">
                  No accounts configured yet. Add a Google account to bypass Google Drive quota limits.
                </div>
              ) : (
                <div className="space-y-2">
                  {accountsList.map((acc) => (
                    <div
                      key={acc.id}
                      className="p-3.5 rounded-lg bg-white/[0.02] border border-[var(--line-05)] flex items-center justify-between"
                    >
                      <div className="space-y-0.5">
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-semibold text-white">{acc.label}</span>
                          <span className="px-1.5 py-0.2 rounded bg-[var(--surface-10)] text-[9.5px] text-[var(--ink-60)] uppercase font-mono">
                            {acc.provider_id}
                          </span>
                        </div>
                        <span className="text-[10px] text-[var(--ink-40)] font-mono">
                          ID: {acc.id.slice(0, 8)}... • State: {acc.state || "healthy"}
                        </span>
                      </div>

                      <div className="flex items-center gap-3">
                        <button
                          onClick={() => handleDeleteAccount(acc.id)}
                          className="text-[11px] text-red-400/70 hover:text-red-400 hover:underline"
                        >
                          Remove
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
