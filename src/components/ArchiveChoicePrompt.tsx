import React, { useState } from "react";
import type { ArchiveChoice } from "../lib/engineEvents";

export function ArchiveChoicePrompt({
  choice,
  onExtract,
  onKeep,
}: {
  choice: ArchiveChoice;
  onExtract: () => Promise<void>;
  onKeep: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = choice.inputPath.split(/[/\\]/).pop() || choice.inputPath;

  const handleExtract = async () => {
    setBusy(true);
    setError(null);
    try {
      await onExtract();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Archive extraction choice"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(0,0,0,0.55)",
      }}
    >
      <div
        className="flex flex-col gap-3 p-4 rounded-lg"
        style={{
          width: "min(420px, calc(100vw - 32px))",
          background: "#1b1f27",
          border: "1px solid var(--line-10)",
          boxShadow: "0 12px 40px rgba(0,0,0,0.5)",
        }}
      >
        <div>
          <h2 style={{ fontSize: "12.5px", fontWeight: 700, color: "var(--ink-90)", margin: 0 }}>
            Archive package ready
          </h2>
          <p style={{ fontSize: "10.5px", color: "var(--ink-60)", margin: "4px 0 0", lineHeight: 1.5 }}>
            The engine finished downloading <strong style={{ color: "var(--ink-80)" }}>{name}</strong> but
            automatic extraction was disabled for this download. Extract it now?
          </p>
          <p style={{ fontSize: "9.5px", color: "var(--ink-35)", margin: "6px 0 0", wordBreak: "break-all" }}>
            {choice.inputPath}
          </p>
        </div>
        {error && (
          <p role="alert" style={{ fontSize: "10px", color: "var(--danger)", margin: 0 }}>
            {error}
          </p>
        )}
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onKeep}
            disabled={busy}
            style={{
              fontSize: "11px",
              fontWeight: 600,
              padding: "5px 12px",
              borderRadius: "5px",
              background: "var(--surface-08)",
              color: "var(--ink-70)",
              cursor: busy ? "not-allowed" : "pointer",
            }}
          >
            Keep archive
          </button>
          <button
            type="button"
            onClick={() => void handleExtract()}
            disabled={busy}
            style={{
              fontSize: "11px",
              fontWeight: 600,
              padding: "5px 12px",
              borderRadius: "5px",
              background: busy ? "rgba(37,99,235,0.4)" : "#2563eb",
              color: "#fff",
              cursor: busy ? "wait" : "pointer",
            }}
          >
            {busy ? "Extracting…" : "Extract now"}
          </button>
        </div>
      </div>
    </div>
  );
}
