import React, { useState } from "react";
import { Icon, ic } from "../icons";
import { installPythonWinget, checkPythonEnvironment, openPath } from "../../api";

export function PythonSetupModal({
  onDismiss,
  onSuccess,
}: {
  onDismiss?: () => void;
  onSuccess?: () => void;
}) {
  const [installing, setInstalling] = useState(false);
  const [checking, setChecking] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [installedVersion, setInstalledVersion] = useState<string | null>(null);

  const handleInstallWinget = async () => {
    setInstalling(true);
    setErrorMessage(null);
    setStatusMessage("Launching Windows Package Manager (winget)... Please approve the administrator prompt if it appears.");
    try {
      await installPythonWinget();
      setStatusMessage("Installation command launched in background. Waiting for installation to complete...");
      let attempts = 0;
      const interval = window.setInterval(async () => {
        attempts++;
        try {
          const res = await checkPythonEnvironment();
          if (res.installed) {
            window.clearInterval(interval);
            setInstalling(false);
            setInstalledVersion(res.version);
            setStatusMessage(`Python ${res.version || "3"} detected successfully!`);
            setTimeout(() => {
              onSuccess?.();
            }, 1500);
          }
        } catch {
          // Keep polling
        }
        if (attempts > 30) {
          window.clearInterval(interval);
          setInstalling(false);
          setStatusMessage("Installer has been dispatched. Click 'Check Again' once the installer finishes.");
        }
      }, 3000);
    } catch (err) {
      setInstalling(false);
      setErrorMessage(err instanceof Error ? err.message : String(err));
    }
  };

  const handleCheckAgain = async () => {
    setChecking(true);
    setErrorMessage(null);
    try {
      const res = await checkPythonEnvironment();
      if (res.installed || res.can_run) {
        setInstalledVersion(res.version);
        setStatusMessage(`Python ${res.version || "3"} is ready! Launching engine...`);
        setTimeout(() => {
          onSuccess?.();
        }, 1000);
      } else {
        setErrorMessage("Python was not detected yet. Make sure installation finished and Python is added to your PATH.");
      }
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="modal-overlay" style={{ zIndex: 9999, background: "rgba(10, 10, 16, 0.88)", backdropFilter: "blur(12px)" }}>
      <div className="modal-panel" style={{ maxWidth: 520, border: "1px solid var(--line-12)", boxShadow: "0 24px 60px rgba(0,0,0,0.6)" }}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-5 shrink-0" style={{ borderBottom: "1px solid var(--line-07)" }}>
          <div className="flex items-center gap-3">
            <div
              className="w-8 h-8 rounded-lg flex items-center justify-center"
              style={{ background: "linear-gradient(135deg, #3b82f6, var(--accent))", boxShadow: "0 2px 10px rgba(0,120,212,0.4)" }}
            >
              <Icon d={ic.cpu} size={18} style={{ color: "#ffffff" }} />
            </div>
            <div>
              <h3 style={{ fontSize: "14px", fontWeight: 700, color: "var(--text-primary)" }}>
                Python 3 Engine Setup Required
              </h3>
              <p style={{ fontSize: "10.5px", color: "var(--text-muted)", marginTop: "2px" }}>
                MossDL high-performance multi-threaded download engine
              </p>
            </div>
          </div>
          {onDismiss && (
            <button
              onClick={onDismiss}
              className="p-1 rounded text-[var(--ink-40)] hover:text-[var(--ink-90)] hover:bg-[var(--surface-10)] transition-colors"
            >
              <Icon d={ic.close} size={16} />
            </button>
          )}
        </div>

        {/* Content */}
        <div className="px-6 py-5 space-y-4">
          <p style={{ fontSize: "12px", color: "var(--ink-85)", lineHeight: 1.6 }}>
            MossDL requires <strong>Python 3.10+</strong> on your system to power its acceleration engine, captcha solving cascade, shortlink decoders, and multi-threaded connection manager.
          </p>

          {statusMessage && (
            <div className="p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/25 flex items-start gap-2.5">
              <Icon d={ic.check} size={14} className="text-emerald-400 shrink-0 mt-0.5" />
              <p style={{ fontSize: "11px", color: "var(--success)", lineHeight: 1.5 }}>
                {statusMessage}
              </p>
            </div>
          )}

          {errorMessage && (
            <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/25 flex items-start gap-2.5">
              <Icon d={ic.alertTriangle} size={14} className="text-rose-400 shrink-0 mt-0.5" />
              <p style={{ fontSize: "11px", color: "var(--danger)", lineHeight: 1.5 }}>
                {errorMessage}
              </p>
            </div>
          )}

          <div className="p-4 rounded-xl bg-white/[0.03] border border-[var(--line-10)] space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <p style={{ fontSize: "12px", fontWeight: 600, color: "#ffffff" }}>
                  Recommended: 1-Click Windows Install
                </p>
                <p style={{ fontSize: "10px", color: "var(--ink-40)", marginTop: "2px" }}>
                  Uses official Microsoft winget to install standard Python 3.12 cleanly.
                </p>
              </div>
              <button
                disabled={installing || Boolean(installedVersion)}
                onClick={handleInstallWinget}
                className="px-4 py-2 rounded-lg font-semibold text-xs text-white transition-all flex items-center gap-2"
                style={{
                  background: "linear-gradient(135deg, var(--accent), #005a9e)",
                  boxShadow: "0 2px 8px rgba(0,120,212,0.35)",
                  opacity: installing || Boolean(installedVersion) ? 0.6 : 1,
                }}
              >
                {installing ? (
                  <>
                    <Icon d={ic.refreshCw} size={12} className="spin" />
                    <span>Installing…</span>
                  </>
                ) : (
                  <>
                    <Icon d={ic.zap} size={13} />
                    <span>Install Python 3</span>
                  </>
                )}
              </button>
            </div>
          </div>

          <div className="p-4 rounded-xl bg-white/[0.02] border border-[var(--line-05)] space-y-2">
            <p style={{ fontSize: "11px", fontWeight: 600, color: "var(--ink-70)" }}>
              Alternative: Manual Install
            </p>
            <p style={{ fontSize: "10px", color: "var(--ink-40)", lineHeight: 1.5 }}>
              You can also download official Python from python.org or Microsoft Store. Please ensure <strong>"Add python.exe to PATH"</strong> is checked during setup.
            </p>
            <div className="flex items-center gap-3 pt-1">
              <button
                type="button"
                onClick={() => {
                  void openPath("https://www.python.org/downloads/windows/").catch(() => {});
                }}
                className="text-xs text-blue-400 hover:text-blue-300 hover:underline flex items-center gap-1"
              >
                <Icon d={ic.externalLink} size={11} />
                <span>Open Python.org Download Page</span>
              </button>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div
          className="flex items-center justify-between px-6 py-4 shrink-0 bg-white/[0.015]"
          style={{ borderTop: "1px solid var(--line-07)" }}
        >
          <span style={{ fontSize: "10.5px", color: "var(--ink-40)" }}>
            Need help? Check Dev Console (F12)
          </span>
          <div className="flex items-center gap-3">
            <button
              disabled={checking}
              onClick={handleCheckAgain}
              className="px-4 py-2 rounded-lg font-medium text-xs text-white bg-[var(--surface-10)] hover:bg-[var(--surface-20)] transition-colors flex items-center gap-2"
            >
              <Icon d={ic.refreshCw} size={12} className={checking ? "spin" : ""} />
              <span>Check Again & Launch</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
