import React, { useState, useRef, useEffect } from "react";
import type { Page, MenuId } from "../types";
import { Icon, ic } from "../icons";
import { devLog } from "../../api";

export interface MenuBarProps {
  setPage: (p: Page) => void;
  onAddUrl: () => void;
  onPauseAll: () => void;
  onResumeAll: () => void;
  onClearCompleted?: () => void;
  onToggleClipboard?: () => void;
  clipboardWatcherEnabled?: boolean;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onZoomReset: () => void;
  onQuit?: () => void;
  onToggleDevLogs?: () => void;
  onOpenSetupWizard?: () => void;
  pauseAllEnabled?: boolean;
  resumeAllEnabled?: boolean;
}

export function MenuBar({
  setPage,
  onAddUrl,
  onPauseAll,
  onResumeAll,
  onClearCompleted,
  onToggleClipboard,
  clipboardWatcherEnabled = false,
  onZoomIn,
  onZoomOut,
  onZoomReset,
  onQuit,
  onToggleDevLogs,
  onOpenSetupWizard,
  pauseAllEnabled = true,
  resumeAllEnabled = true,
}: MenuBarProps) {
  const [open, setOpen] = useState<MenuId | null>(null);
  const [modalDialog, setModalDialog] = useState<"about" | "shortcuts" | null>(null);
  const barRef = useRef<HTMLDivElement>(null);

  const IS_DEV = import.meta.env.DEV;

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) setOpen(null);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const toggle = (id: MenuId) => setOpen((prev) => (prev === id ? null : id));

  const Item = ({
    icon,
    label,
    kbd,
    onClick,
    check,
    disabled = false,
  }: {
    icon?: string;
    label: string;
    kbd?: string;
    onClick?: () => void;
    check?: boolean;
    disabled?: boolean;
  }) => (
    <button
      type="button"
      className={`menu-item${disabled ? " disabled" : ""}`}
      aria-disabled={disabled}
      role="menuitem"
      data-tauri-drag-region="false"
      title={disabled ? "This action is not available yet" : undefined}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => {
        e.stopPropagation();
        if (!disabled && onClick) {
          onClick();
          setOpen(null);
        }
      }}
    >
      <span className="menu-item-icon w-3.5 flex justify-center">
        {check !== undefined ? (
          <span style={{ width: 14 }}>
            {check ? <Icon d={ic.check} size={12} style={{ color: "var(--accent-light)" }} /> : null}
          </span>
        ) : icon ? (
          <Icon d={icon} size={12} />
        ) : (
          <span style={{ width: 14 }} />
        )}
      </span>
      <span className="flex-1">{label}</span>
      {kbd && <span className="menu-item-kbd">{kbd}</span>}
    </button>
  );
  const Sep = () => <div className="menu-sep" />;

  const menus: { id: MenuId; label: string }[] = [
    { id: "file", label: "File" },
    { id: "view", label: "View" },
    { id: "tools", label: "Tools" },
    ...(IS_DEV ? [{ id: "dev" as MenuId, label: "Dev" }] : []),
    { id: "help", label: "Help" },
  ];

  return (
    <>
      <div ref={barRef} className="menu-bar" data-tauri-drag-region="false" role="menubar">
        {menus.map((m) => (
          <div key={m.id} style={{ position: "relative" }} role="none">
            <button
              type="button"
              className={`menu-trigger ${open === m.id ? "open" : ""}`}
              data-tauri-drag-region="false"
              aria-haspopup="menu"
              aria-expanded={open === m.id}
              onClick={() => toggle(m.id)}
            >
              {m.label}
            </button>
            {open === m.id && (
              <div
                className="menu-dropdown"
                role="menu"
                data-tauri-drag-region="false"
                onMouseDown={(e) => e.stopPropagation()}
              >
                {m.id === "file" && (
                  <>
                    <Item icon={ic.plus} label="Add URL…" kbd="Ctrl+N" onClick={onAddUrl} />
                    <Item
                      icon={ic.fileDown}
                      label="Open torrent file…"
                      kbd="Ctrl+O"
                      onClick={() => setPage("explore")}
                    />
                    <Item
                      icon={ic.clipboardList}
                      label="Import download list…"
                      onClick={() => setPage("explore")}
                    />
                    <Sep />
                    <Item
                      icon={ic.pause}
                      label="Pause all"
                      onClick={onPauseAll}
                      disabled={!pauseAllEnabled}
                    />
                    <Item
                      icon={ic.play}
                      label="Resume all"
                      onClick={onResumeAll}
                      disabled={!resumeAllEnabled}
                    />
                    <Sep />
                    <Item icon={ic.fileDown} label="Export history…" onClick={() => setPage("history")} />
                    <Sep />
                    <Item icon={ic.close} label="Quit" kbd="Alt+F4" onClick={onQuit} disabled={!onQuit} />
                  </>
                )}
                {m.id === "view" && (
                  <>
                    <Item icon={ic.download} label="Downloads" onClick={() => setPage("downloads")} />
                    <Item icon={ic.history} label="History" onClick={() => setPage("history")} />
                    <Item icon={ic.settings} label="Settings" onClick={() => setPage("settings")} />
                    <Item icon={ic.terminal} label="Dev Logs Console" kbd="Ctrl+`" onClick={() => onToggleDevLogs?.()} />
                    <Sep />
                    <Item icon={ic.plus} label="Zoom in" kbd="Ctrl++" onClick={onZoomIn} />
                    <Item icon={ic.minimize} label="Zoom out" kbd="Ctrl+−" onClick={onZoomOut} />
                    <Item icon={ic.refreshCw} label="Reset zoom" kbd="Ctrl+0" onClick={onZoomReset} />
                  </>
                )}
                {m.id === "tools" && (
                  <>
                    <Item
                      icon={ic.clipboardList}
                      label="Explore"
                      onClick={() => setPage("explore")}
                    />
                    <Item
                      check={clipboardWatcherEnabled}
                      label="Clipboard watcher"
                      onClick={onToggleClipboard}
                      disabled={!onToggleClipboard}
                    />
                    <Sep />
                    <Item
                      icon={ic.trash}
                      label="Clear completed"
                      onClick={onClearCompleted}
                      disabled={!onClearCompleted}
                    />
                    <Sep />
                    <Item icon={ic.cpu} label="Diagnostics" onClick={() => setPage("settings")} />
                    <Item
                      icon={ic.terminal}
                      label="Dev Logs Console"
                      kbd="Ctrl+`"
                      onClick={() => onToggleDevLogs?.()}
                    />
                  </>
                )}
                {m.id === "dev" && IS_DEV && (
                  <>
                    <Item
                      icon={ic.play}
                      label="Launch Setup Wizard…"
                      onClick={() => onOpenSetupWizard?.()}
                      disabled={!onOpenSetupWizard}
                    />
                    <Item
                      icon={ic.refreshCw}
                      label="Reset Wizard State &amp; Rerun"
                      onClick={() => {
                        localStorage.removeItem("onboarding_complete");
                        onOpenSetupWizard?.();
                      }}
                      disabled={!onOpenSetupWizard}
                    />
                    <Sep />
                    <Item
                      icon={ic.terminal}
                      label="Dev Logs Console"
                      kbd="Ctrl+`"
                      onClick={() => onToggleDevLogs?.()}
                    />
                    <Sep />
                    <Item
                      icon={ic.alertTriangle}
                      label="Trigger Test Diagnostic Error"
                      onClick={() => { throw new Error("[DEV] Test diagnostic error from menu"); }}
                    />
                  </>
                )}
                {m.id === "help" && (
                  <>
                    <Item icon={ic.info} label="About MossDL" onClick={() => setModalDialog("about")} />
                    <Sep />
                    <Item
                      icon={ic.externalLink}
                      label="Documentation"
                      kbd="F1"
                      onClick={() => setModalDialog("about")}
                    />
                    <Item
                      icon={ic.alertTriangle}
                      label="Report a bug…"
                      onClick={() => setModalDialog("about")}
                    />
                    <Sep />
                    <Item
                      icon={ic.checkSquare}
                      label="Keyboard shortcuts"
                      kbd="Ctrl+/"
                      onClick={() => setModalDialog("shortcuts")}
                    />
                  </>
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      {modalDialog === "about" && (
        <div className="modal-overlay" onClick={() => setModalDialog(null)}>
          <div className="modal-panel" style={{ maxWidth: 440 }} onClick={(e) => e.stopPropagation()}>
            <div
              className="flex items-center justify-between px-5 py-4 shrink-0"
              style={{ borderBottom: "1px solid var(--line-07)" }}
            >
              <div className="flex items-center gap-2.5">
                <div
                  className="w-3 h-3 rounded-full"
                  style={{ background: "var(--accent)", boxShadow: "0 0 8px var(--accent-glow)" }}
                />
                <h3 style={{ fontSize: "14px", fontWeight: 600, color: "var(--text-primary)" }}>
                  About MossDL
                </h3>
              </div>
              <button
                type="button"
                onClick={() => setModalDialog(null)}
                style={{ color: "var(--ink-40)" }}
              >
                <Icon d={ic.close} size={14} />
              </button>
            </div>
            <div
              className="px-5 py-4 space-y-3"
              style={{ fontSize: "12px", color: "var(--text-secondary)", lineHeight: 1.6 }}
            >
              <p>
                <strong style={{ color: "var(--text-primary)" }}>MossDL v0.1.0</strong> — High-Performance
                Download Manager
              </p>
              <p>
                Powered by a dual-tier architecture: Native desktop shell (Tauri v2 + React 19) coupled to
                an accelerated direct-to-disk multi-stream engine (Rust + Python backend).
              </p>
              <div
                className="rounded-md p-3 space-y-1"
                style={{
                  background: "var(--surface-03)",
                  border: "1px solid var(--line-06)",
                  fontSize: "11px",
                }}
              >
                <div>
                  <strong>Acceleration:</strong> 32-stream segmented range fetcher
                </div>
                <div>
                  <strong>Engine Core:</strong> transfer-core (Rust native)
                </div>
                <div>
                  <strong>Providers:</strong> Google Drive, Transfer.it, MediaFire, Pixeldrain, 1fichier,
                  Krakenfiles, CyberDrop, Mega, Direct HTTP/S
                </div>
              </div>
            </div>
            <div
              className="flex justify-end px-5 py-3 shrink-0"
              style={{ borderTop: "1px solid var(--line-07)" }}
            >
              <button
                type="button"
                className="action-btn primary"
                onClick={() => setModalDialog(null)}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {modalDialog === "shortcuts" && (
        <div className="modal-overlay" onClick={() => setModalDialog(null)}>
          <div className="modal-panel" style={{ maxWidth: 480 }} onClick={(e) => e.stopPropagation()}>
            <div
              className="flex items-center justify-between px-5 py-4 shrink-0"
              style={{ borderBottom: "1px solid var(--line-07)" }}
            >
              <h3 style={{ fontSize: "14px", fontWeight: 600, color: "var(--text-primary)" }}>
                Keyboard Shortcuts
              </h3>
              <button
                type="button"
                onClick={() => setModalDialog(null)}
                style={{ color: "var(--ink-40)" }}
              >
                <Icon d={ic.close} size={14} />
              </button>
            </div>
            <div className="px-5 py-3 space-y-2" style={{ fontSize: "12px" }}>
              {[
                { key: "Ctrl + N", desc: "Open Add URL modal" },
                { key: "Ctrl + +", desc: "Zoom in" },
                { key: "Ctrl + −", desc: "Zoom out" },
                { key: "Ctrl + 0", desc: "Reset zoom" },
                { key: "F12", desc: "Developer tools & console" },
                { key: "Alt + F4", desc: "Quit application" },
              ].map((s, i) => (
                <div
                  key={i}
                  className="flex items-center justify-between py-1.5"
                  style={{ borderBottom: "1px solid var(--line-05)" }}
                >
                  <span style={{ color: "var(--text-secondary)" }}>{s.desc}</span>
                  <span
                    className="menu-item-kbd"
                    style={{ background: "var(--surface-06)", padding: "2px 6px", borderRadius: 4 }}
                  >
                    {s.key}
                  </span>
                </div>
              ))}
            </div>
            <div
              className="flex justify-end px-5 py-3 shrink-0"
              style={{ borderTop: "1px solid var(--line-07)" }}
            >
              <button
                type="button"
                className="action-btn primary"
                onClick={() => setModalDialog(null)}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
