// One confirmation dialog for the whole app, in the shared modal style so it
// reads as part of the window rather than a browser alert.
import React, { useEffect, useState } from "react";
import { Icon, ic } from "../icons";

export type ConfirmTone = "danger" | "normal";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** The consequence, in plain words. Shown as the dialog body. */
  body: React.ReactNode;
  confirmLabel: string;
  confirmIcon?: string;
  cancelLabel?: string;
  tone?: ConfirmTone;
  /** When set, the dialog offers to stop asking and reports the choice. */
  rememberLabel?: string;
  onConfirm: (remember: boolean) => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  confirmIcon,
  cancelLabel = "Cancel",
  tone = "normal",
  rememberLabel,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const [remember, setRemember] = useState(false);

  useEffect(() => {
    if (open) setRemember(false);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCancel();
      } else if (event.key === "Enter") {
        event.preventDefault();
        onConfirm(remember);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, remember, onConfirm, onCancel]);

  if (!open) return null;
  const accent = tone === "danger" ? "var(--danger)" : "var(--accent-light)";

  return (
    <div className="modal-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) onCancel(); }}>
      <div className="modal-panel flex flex-col" role="alertdialog" aria-modal="true" aria-label={title} style={{ width: 420 }}>
        <div className="flex items-start gap-3 px-5 pt-4 pb-3">
          <div
            className="flex items-center justify-center shrink-0"
            style={{
              width: 30,
              height: 30,
              borderRadius: "var(--radius-control)",
              background: tone === "danger" ? "rgba(248,113,113,0.14)" : "var(--accent-soft)",
              color: accent,
            }}
          >
            <Icon d={ic.alertTriangle} size={16} />
          </div>
          <div className="min-w-0 flex-1">
            <h2 style={{ margin: 0, fontSize: "13px", fontWeight: 700, color: "var(--ink-90)" }}>{title}</h2>
            <div style={{ marginTop: 4, fontSize: "11.5px", color: "var(--ink-70)", lineHeight: 1.55 }}>{body}</div>
          </div>
        </div>

        {rememberLabel && (
          <label
            className="flex items-center gap-2 mx-5 mb-3 px-3 py-2 cursor-pointer select-none"
            style={{
              fontSize: "11px",
              color: "var(--ink-60)",
              background: "var(--surface-03)",
              border: "1px solid var(--line-06)",
              borderRadius: "var(--radius-control)",
            }}
          >
            <input
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
              style={{ width: 13, height: 13, accentColor: "var(--accent)", cursor: "pointer" }}
            />
            {rememberLabel}
          </label>
        )}

        <div
          className="flex items-center justify-end gap-2 px-5 py-3"
          style={{ borderTop: "1px solid var(--line-06)", background: "var(--surface-02)" }}
        >
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-1.5 rounded-md"
            style={{
              fontSize: "11.5px",
              fontWeight: 600,
              color: "var(--ink-80)",
              background: "var(--surface-08)",
              border: "1px solid var(--line-09)",
            }}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            autoFocus
            onClick={() => onConfirm(remember)}
            className="px-4 py-1.5 rounded-md flex items-center gap-1.5"
            style={{
              fontSize: "11.5px",
              fontWeight: 700,
              color: tone === "danger" ? "#fff" : "var(--on-accent)",
              background: tone === "danger" ? "var(--danger)" : "var(--accent)",
              border: "1px solid transparent",
            }}
          >
            {confirmIcon && <Icon d={confirmIcon} size={12} />}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
