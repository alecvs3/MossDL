import React, { useState, useEffect, useCallback } from "react";
import { emitLog } from "../../lib/telemetry";
import { useClearcoteInstall } from "../ui/useClearcoteInstall";
import { Icon, ic } from "../icons";
import {
  checkClearcoteStatus,
  openPath,
  type ClearcoteStatus,
} from "../../api";

import { THEMES } from "../themes";

export interface WelcomeWizardProps {
  onDismiss: () => void;
  onSettingChange?: (tab: "general" | "appearance" | "network", key: string, value: string | boolean) => void;
  currentTheme?: string;
  acrylic?: boolean;
}

export function WelcomeWizard({
  onDismiss,
  onSettingChange,
  currentTheme = "midnight",
  acrylic = true,
}: WelcomeWizardProps) {
  const [step, setStep] = useState<0 | 1 | 2>(0);
  const [clearcote, setClearcote] = useState<ClearcoteStatus | null>(null);
  const [selectedTheme, setSelectedTheme] = useState<string>(currentTheme);
  const [selectedAcrylic, setSelectedAcrylic] = useState<boolean>(acrylic);
  const [adblockLists, setAdblockLists] = useState(true);

  useEffect(() => {
    checkClearcoteStatus().then(setClearcote,
      (e: unknown) => void emitLog("WARNING", "ui:wizard", "Clearcote status unavailable", {}, e));
  }, []);

  // Real progress from the engine's download; the wizard moves on once it is verified.
  const { install, start: handleInstallClearcote } = useClearcoteInstall((done) => {
    if (done.status) setClearcote(done.status);
  });
  const installing = install.running;
  const installProgress = { pct: install.percent, msg: install.message };
  const installError = install.error;


  const selectTheme = useCallback(
    (themeId: string, accent: string) => {
      setSelectedTheme(themeId);
      onSettingChange?.("appearance", "themeName", themeId);
      onSettingChange?.("appearance", "colorAccent", accent);
    },
    [onSettingChange]
  );

  const toggleAcrylic = useCallback(() => {
    const next = !selectedAcrylic;
    setSelectedAcrylic(next);
    onSettingChange?.("appearance", "acrylic", next);
  }, [selectedAcrylic, onSettingChange]);

  const finishWizard = () => {
    try {
      localStorage.setItem("onboarding_dismissed", "true");
    } catch {}
    onDismiss();
  };

  const openChromeStore = () => {
    void openPath("https://chromewebstore.google.com/detail/mossdl-capture/dknlgnaknfaoflbmobdknjfjdagieccm");
  };

  const openFirefoxStore = () => {
    void openPath("https://addons.mozilla.org/firefox/addon/mossdl-capture/");
  };

  const openDocs = () => {
    void openPath("https://mossdownloader.com/docs/extension/");
  };

  const openChromeExtensions = async () => {
    await openPath("chrome://extensions").catch(() => {});
  };

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center p-6 select-none"
      style={{
        background: "rgba(8, 10, 15, 0.84)",
        backdropFilter: "blur(24px)",
        WebkitBackdropFilter: "blur(24px)",
      }}
    >
      <div
        className="w-full max-w-2xl rounded-2xl overflow-hidden shadow-2xl flex flex-col"
        style={{
          background: "linear-gradient(180deg, rgba(20, 24, 34, 0.96) 0%, rgba(12, 15, 22, 0.98) 100%)",
          border: "1px solid var(--line-12)",
          boxShadow: "0 28px 75px rgba(0, 0, 0, 0.8), 0 0 1px rgba(255, 255, 255, 0.2) inset",
        }}
      >
        {/* Wizard Header */}
        <div className="px-8 pt-6 pb-4 border-b border-white/[0.08] flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-blue-600 to-indigo-500 flex items-center justify-center text-white shadow-lg shadow-blue-500/30">
              <Icon d={step === 0 ? ic.shield : step === 1 ? ic.palette : ic.sparkles} size={18} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-bold text-white tracking-wide">Setup Wizard</h2>
                <span className="px-2 py-0.2 rounded-full text-[10px] font-semibold bg-blue-500/20 text-blue-300 border border-blue-500/30">
                  Step {step + 1} of 3
                </span>
              </div>
              <p className="text-[11px] text-[var(--ink-50)]">
                {step === 0 && "High-speed solver & Cloudflare challenge engine"}
                {step === 1 && "Choose your interface theme and window materials"}
                {step === 2 && "Connect browser extension for 1-click downloads"}
              </p>
            </div>
          </div>
          <button
            onClick={finishWizard}
            className="w-7 h-7 rounded-lg bg-[var(--surface-05)] hover:bg-[var(--surface-10)] text-[var(--ink-40)] hover:text-white flex items-center justify-center transition-colors text-xs"
            title="Close setup wizard"
          >
            ✕
          </button>
        </div>

        {/* Step Progress Bar */}
        <div className="flex items-center px-8 pt-4 pb-2 gap-2">
          {[
            { idx: 0, label: "1. Solver Engine" },
            { idx: 1, label: "2. Appearance" },
            { idx: 2, label: "3. Extension" },
          ].map((item) => (
            <div
              key={item.idx}
              onClick={() => !installing && setStep(item.idx as 0 | 1 | 2)}
              className={`flex-1 py-1 px-2 rounded-lg text-center text-[10.5px] font-medium cursor-pointer transition-all ${
                step === item.idx
                  ? "bg-blue-500/20 text-blue-300 border border-blue-500/40"
                  : step > item.idx
                  ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                  : "bg-white/[0.03] text-[var(--ink-40)] border border-white/[0.05]"
              }`}
            >
              {item.label}
            </div>
          ))}
        </div>

        {/* Step Content */}
        <div className="p-8 space-y-6 flex-1 overflow-y-auto max-h-[58vh]">
          {/* STEP 0: CLEARCOTE ENGINE */}
          {step === 0 && (
            <div className="space-y-4">
              <div className="p-4 rounded-xl bg-gradient-to-r from-blue-500/15 via-indigo-500/10 to-transparent border border-blue-500/30 flex items-start gap-3.5">
                <div className="w-8 h-8 rounded-lg bg-blue-500/20 text-blue-400 flex items-center justify-center shrink-0 mt-0.5">
                  <Icon d={ic.shield} size={18} />
                </div>
                <div className="min-w-0">
                  <h3 className="text-xs font-bold text-white">Clearcote Anti-Bot Chromium Runtime</h3>
                  <p className="text-[11px] text-[var(--ink-60)] mt-1 leading-relaxed">
                    Solves Cloudflare Turnstile, interstitial challenges, and bot checks automatically in ~1.9s.
                    Reuses TLS clearance cookies across downloads to prevent HTTP 403 Forbidden errors.
                  </p>
                </div>
              </div>

              {clearcote?.installed ? (
                <div className="p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-between">
                  <div className="flex items-center gap-2.5">
                    <span className="w-6 h-6 rounded-full bg-emerald-500/20 text-emerald-400 flex items-center justify-center text-xs">✓</span>
                    <div>
                      <p className="text-xs font-semibold text-white">Clearcote Chromium Installed & Verified</p>
                      <p className="text-[10.5px] text-[var(--ink-50)]">
                        Version {clearcote.version || "149"} · Size {clearcote.size_mb} MB · Ready
                      </p>
                    </div>
                  </div>
                  <button
                    onClick={() => setStep(1)}
                    className="px-3 py-1.5 rounded-lg text-xs font-semibold text-white bg-blue-600 hover:bg-blue-500 transition-colors"
                  >
                    Next Step →
                  </button>
                </div>
              ) : (
                <div className="space-y-3">
                  {installing && (
                    <div className="p-3.5 rounded-xl bg-[var(--surface-04)] border border-[var(--line-08)] space-y-2">
                      <div className="flex justify-between text-[11px]">
                        <span className="text-blue-300 font-medium">{installProgress.msg}</span>
                        <span className="text-[var(--ink-60)]">{installProgress.pct}%</span>
                      </div>
                      <div className="w-full bg-black/40 h-2 rounded-full overflow-hidden">
                        <div
                          className="bg-gradient-to-r from-blue-500 to-indigo-500 h-full transition-all duration-300 rounded-full"
                          style={{ width: `${installProgress.pct}%` }}
                        />
                      </div>
                    </div>
                  )}

                  {installError && (
                    <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-300 text-xs">
                      {installError}
                    </div>
                  )}

                  <div className="flex items-center justify-between pt-2">
                    <button
                      onClick={() => setStep(1)}
                      className="px-4 py-2 rounded-xl text-xs font-medium text-[var(--ink-60)] hover:text-white bg-[var(--surface-04)] hover:bg-[var(--surface-08)] transition-colors border border-[var(--line-06)]"
                    >
                      Skip for Now →
                    </button>
                    <button
                      onClick={handleInstallClearcote}
                      disabled={installing}
                      className="px-5 py-2 rounded-xl text-xs font-semibold text-white bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 transition-all shadow-lg shadow-blue-500/25 disabled:opacity-50"
                    >
                      {installing ? "Installing..." : "Install Clearcote (~230MB)"}
                    </button>
                  </div>
                </div>
              )}

              <label className="p-3.5 rounded-xl bg-[var(--surface-04)] border border-[var(--line-08)] flex items-start gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={adblockLists}
                  onChange={(e) => {
                    setAdblockLists(e.target.checked);
                    onSettingChange?.("network", "adblockFullLists", e.target.checked);
                  }}
                />
                <span className="min-w-0">
                  <span className="block text-xs font-semibold text-white">Block ads with uBlock Origin&apos;s filter lists</span>
                  <span className="block text-[11px] text-[var(--ink-60)] mt-0.5 leading-relaxed">
                    Downloads EasyList, EasyPrivacy, uBlock filters and Peter Lowe&apos;s list (about 4 MB, refreshed weekly) so
                    page crawls and the solver skip ads and fake download buttons. Off keeps a small built-in list.
                  </span>
                </span>
              </label>
            </div>
          )}

          {/* STEP 1: THEME SELECTION */}
          {step === 1 && (
            <div className="space-y-4">
              <div>
                <h3 className="text-xs font-bold text-white">Choose your Interface Theme</h3>
                <p className="text-[11px] text-[var(--ink-50)] mt-0.5">
                  Select a color scheme. The application theme updates in real-time.
                </p>
              </div>

              <div className="grid grid-cols-2 md:grid-cols-3 gap-2.5">
                {THEMES.map((theme) => {
                  const active = selectedTheme === theme.id;
                  return (
                    <button
                      key={theme.id}
                      type="button"
                      onClick={() => selectTheme(theme.id, theme.accent)}
                      className={`p-3 rounded-xl text-left transition-all flex flex-col gap-2 ${
                        active
                          ? "bg-blue-500/15 border-2 border-blue-500 shadow-md shadow-blue-500/20"
                          : "bg-[var(--surface-04)] border border-[var(--line-08)] hover:bg-[var(--surface-07)]"
                      }`}
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-xs font-bold text-white">{theme.name}</span>
                        <div
                          className="w-4 h-4 rounded-full border border-white/20 shrink-0"
                          style={{ background: theme.preview.accent }}
                        />
                      </div>
                      <div className="h-6 rounded-md overflow-hidden flex" style={{ background: theme.preview.base }}>
                        <div className="w-1/3 h-full" style={{ background: theme.preview.surface }} />
                        <div className="w-1/3 h-full" style={{ background: theme.preview.accent }} />
                        <div className="w-1/3 h-full" style={{ background: theme.preview.success }} />
                      </div>
                      <span className="text-[10px] text-[var(--ink-50)] leading-tight">{theme.description}</span>
                    </button>
                  );
                })}
              </div>

              <div className="p-3.5 rounded-xl bg-[var(--surface-03)] border border-[var(--line-06)] flex items-center justify-between">
                <div>
                  <p className="text-xs font-semibold text-white">Frosted Glass Blur</p>
                  <p className="text-[10.5px] text-[var(--ink-50)]">
                    Use Acrylic blur and Windows Mica glass effect behind panels
                  </p>
                </div>
                <input
                  type="checkbox"
                  checked={selectedAcrylic}
                  onChange={toggleAcrylic}
                  className="accent-blue-500 w-4 h-4 cursor-pointer"
                />
              </div>

              <div className="flex items-center justify-between pt-2">
                <button
                  onClick={() => setStep(0)}
                  className="px-4 py-2 rounded-xl text-xs font-medium text-[var(--ink-60)] hover:text-white bg-[var(--surface-04)] hover:bg-[var(--surface-08)] transition-colors border border-[var(--line-06)]"
                >
                  ← Back
                </button>
                <button
                  onClick={() => setStep(2)}
                  className="px-5 py-2 rounded-xl text-xs font-semibold text-white bg-blue-600 hover:bg-blue-500 transition-colors shadow-lg shadow-blue-500/20"
                >
                  Next: Browser Extension →
                </button>
              </div>
            </div>
          )}

          {/* STEP 2: BROWSER EXTENSION */}
          {step === 2 && (
            <div className="space-y-4">
              <div className="p-4 rounded-xl bg-gradient-to-r from-purple-500/15 via-indigo-500/10 to-transparent border border-purple-500/30 flex items-start gap-3.5">
                <div className="w-8 h-8 rounded-lg bg-purple-500/20 text-purple-400 flex items-center justify-center shrink-0 mt-0.5">
                  <Icon d={ic.globe} size={18} />
                </div>
                <div className="min-w-0">
                  <h3 className="text-xs font-bold text-white">MossDL Browser Extension (MossDL Capture)</h3>
                  <p className="text-[11px] text-[var(--ink-60)] mt-1 leading-relaxed">
                    Intercept file downloads in Chrome, Edge, Brave, and Firefox automatically. Right-click any link or page to
                    capture media with MossDL, or sync session cookies for authenticated hosters.
                  </p>
                </div>
              </div>

              {/* Official Store Listings */}
              <div className="space-y-2">
                <div className="text-[10px] font-semibold text-[var(--ink-60)] uppercase tracking-wider px-0.5">
                  Official Web Stores
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                  <button
                    type="button"
                    onClick={openChromeStore}
                    className="p-3.5 rounded-xl bg-[var(--surface-04)] hover:bg-[var(--surface-07)] border border-[var(--line-08)] hover:border-blue-500/40 text-left transition-all group flex flex-col justify-between gap-2"
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-1.5">
                        <span className="text-xs font-bold text-white group-hover:text-blue-400 transition-colors">Chrome Web Store</span>
                        <Icon d={ic.externalLink} size={11} className="text-[var(--ink-40)] group-hover:text-blue-400 transition-colors" />
                      </div>
                      <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30">
                        Pending review
                      </span>
                    </div>
                    <p className="text-[10.5px] text-[var(--ink-50)]">For Google Chrome, Microsoft Edge, Brave, and Opera</p>
                  </button>

                  <button
                    type="button"
                    onClick={openFirefoxStore}
                    className="p-3.5 rounded-xl bg-[var(--surface-04)] hover:bg-[var(--surface-07)] border border-[var(--line-08)] hover:border-orange-500/40 text-left transition-all group flex flex-col justify-between gap-2"
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-1.5">
                        <span className="text-xs font-bold text-white group-hover:text-orange-400 transition-colors">Firefox Add-ons</span>
                        <Icon d={ic.externalLink} size={11} className="text-[var(--ink-40)] group-hover:text-orange-400 transition-colors" />
                      </div>
                      <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30">
                        Pending review
                      </span>
                    </div>
                    <p className="text-[10.5px] text-[var(--ink-50)]">For Mozilla Firefox and Gecko-based browsers</p>
                  </button>
                </div>
              </div>

              {/* Developer Mode / Unpacked Installation */}
              <div className="p-3.5 rounded-xl bg-[var(--surface-03)] border border-[var(--line-06)] space-y-2.5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-semibold text-white">Manual / Developer Mode Installation</span>
                    <span className="px-1.5 py-0.5 rounded text-[9.5px] font-medium bg-blue-500/15 text-blue-300 border border-blue-500/25">
                      Available now
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={openDocs}
                    className="text-[11px] text-blue-400 hover:text-blue-300 flex items-center gap-1 transition-colors"
                  >
                    Documentation <Icon d={ic.externalLink} size={11} />
                  </button>
                </div>
                <p className="text-[10.5px] text-[var(--ink-50)] leading-relaxed">
                  While store submissions complete review, you can load the unpacked extension in developer mode or follow the setup guide on mossdownloader.com.
                </p>
                <div className="flex items-center gap-2 pt-1">
                  <button
                    type="button"
                    onClick={openChromeExtensions}
                    className="flex-1 py-1.5 px-3 rounded-lg text-xs font-medium text-white bg-[var(--surface-05)] hover:bg-[var(--surface-08)] border border-[var(--line-08)] flex items-center justify-center gap-1.5 transition-colors"
                  >
                    <Icon d={ic.externalLink} size={12} />
                    Open chrome://extensions
                  </button>
                  <button
                    type="button"
                    onClick={openDocs}
                    className="flex-1 py-1.5 px-3 rounded-lg text-xs font-medium text-white bg-[var(--surface-05)] hover:bg-[var(--surface-08)] border border-[var(--line-08)] flex items-center justify-center gap-1.5 transition-colors"
                  >
                    <Icon d={ic.globe} size={12} />
                    View Setup Guide
                  </button>
                </div>
              </div>

              <div className="flex items-center justify-between pt-2">
                <button
                  onClick={() => setStep(1)}
                  className="px-4 py-2 rounded-xl text-xs font-medium text-[var(--ink-60)] hover:text-white bg-[var(--surface-04)] hover:bg-[var(--surface-08)] transition-colors border border-[var(--line-06)]"
                >
                  ← Back
                </button>
                <div className="flex items-center gap-2">
                  <button
                    onClick={finishWizard}
                    className="px-4 py-2 rounded-xl text-xs font-medium text-[var(--ink-60)] hover:text-white bg-[var(--surface-04)] hover:bg-[var(--surface-08)] transition-colors border border-[var(--line-06)]"
                  >
                    Skip
                  </button>
                  <button
                    onClick={finishWizard}
                    className="px-5 py-2 rounded-xl text-xs font-semibold text-white bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 transition-all shadow-lg shadow-blue-500/25"
                  >
                    Finish Setup & Start
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
