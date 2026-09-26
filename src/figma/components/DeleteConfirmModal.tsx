import React, { useEffect, useState } from "react";
import { Icon, ic } from "../icons";

interface DeleteConfirmModalProps {
  isOpen: boolean;
  itemNames: string[];
  onConfirm: (deleteFilesFromDisk: boolean) => void;
  onCancel: () => void;
}

export const DeleteConfirmModal: React.FC<DeleteConfirmModalProps> = ({
  isOpen,
  itemNames,
  onConfirm,
  onCancel,
}) => {
  const [deleteFiles, setDeleteFiles] = useState(false);

  useEffect(() => {
    if (!isOpen) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      } else if (e.key === "Enter") {
        e.preventDefault();
        onConfirm(deleteFiles);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, deleteFiles, onConfirm, onCancel]);

  if (!isOpen || itemNames.length === 0) return null;

  const count = itemNames.length;
  const isSingle = count === 1;
  const displayName = isSingle ? itemNames[0] : `${count} selected downloads`;

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 9999,
        background: "rgba(0, 0, 0, 0.65)",
        backdropFilter: "blur(4px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px",
      }}
      onClick={onCancel}
    >
      <div
        style={{
          width: "100%",
          maxWidth: "420px",
          background: "var(--surface-strong)",
          border: "1px solid var(--line-12)",
          borderRadius: "8px",
          boxShadow: "0 20px 25px -5px rgba(0, 0, 0, 0.6), 0 10px 10px -5px rgba(0, 0, 0, 0.4)",
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "12px",
            padding: "16px 20px",
            borderBottom: "1px solid var(--line-07)",
          }}
        >
          <div
            style={{
              width: "32px",
              height: "32px",
              borderRadius: "6px",
              background: "rgba(239, 68, 68, 0.15)",
              color: "#ef4444",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            <Icon d={ic.alertTriangle} size={18} />
          </div>

          <div style={{ flex: 1, minWidth: 0 }}>
            <h2
              style={{
                margin: 0,
                fontSize: "13px",
                fontWeight: 700,
                color: "#fff",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {isSingle ? "Delete Download?" : `Delete ${count} Downloads?`}
            </h2>
            <p
              style={{
                margin: "2px 0 0 0",
                fontSize: "11px",
                color: "var(--ink-45)",
              }}
            >
              This action will remove the transfer from your list.
            </p>
          </div>
        </div>

        {/* Content Body */}
        <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: "12px" }}>
          {isSingle ? (
            <p
              style={{
                margin: 0,
                fontSize: "12px",
                color: "var(--ink-80)",
                lineHeight: 1.5,
                wordBreak: "break-word",
              }}
            >
              Are you sure you want to delete <strong style={{ color: "#fff" }}>"{displayName}"</strong>?
            </p>
          ) : (
            <>
              <p
                style={{
                  margin: 0,
                  fontSize: "12px",
                  color: "var(--ink-80)",
                  lineHeight: 1.5,
                }}
              >
                Are you sure you want to delete these <strong style={{ color: "#fff" }}>{count}</strong> downloads?
              </p>

              <div
                style={{
                  maxHeight: "100px",
                  overflowY: "auto",
                  background: "var(--surface-03)",
                  border: "1px solid var(--line-06)",
                  borderRadius: "4px",
                  padding: "6px 10px",
                  display: "flex",
                  flexDirection: "column",
                  gap: "4px",
                }}
              >
                {itemNames.slice(0, 5).map((name, idx) => (
                  <span
                    key={idx}
                    style={{
                      fontSize: "10px",
                      color: "var(--ink-60)",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      fontFamily: "'JetBrains Mono', monospace",
                    }}
                  >
                    • {name}
                  </span>
                ))}
                {count > 5 && (
                  <span style={{ fontSize: "9.5px", color: "var(--ink-35)", fontStyle: "italic" }}>
                    ...and {count - 5} more files
                  </span>
                )}
              </div>
            </>
          )}

          {/* Delete files from disk toggle */}
          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              fontSize: "11px",
              color: "var(--ink-70)",
              cursor: "pointer",
              userSelect: "none",
              marginTop: "4px",
            }}
          >
            <input
              type="checkbox"
              checked={deleteFiles}
              onChange={(e) => setDeleteFiles(e.target.checked)}
              style={{
                width: "14px",
                height: "14px",
                accentColor: "#ef4444",
                cursor: "pointer",
              }}
            />
            Also delete downloaded files from disk
          </label>
        </div>

        {/* Footer Actions */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "flex-end",
            gap: "8px",
            padding: "12px 20px",
            background: "var(--surface-02)",
            borderTop: "1px solid var(--line-06)",
          }}
        >
          <button
            type="button"
            onClick={onCancel}
            style={{
              padding: "6px 14px",
              fontSize: "11px",
              fontWeight: 600,
              color: "var(--ink-80)",
              background: "var(--surface-08)",
              border: "none",
              borderRadius: "4px",
              cursor: "pointer",
            }}
            onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-12)"; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "var(--surface-08)"; }}
          >
            Cancel
          </button>

          <button
            type="button"
            onClick={() => onConfirm(deleteFiles)}
            style={{
              padding: "6px 16px",
              fontSize: "11px",
              fontWeight: 700,
              color: "#fff",
              background: "#ef4444",
              border: "none",
              borderRadius: "4px",
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              gap: "6px",
            }}
            onMouseEnter={(e) => { e.currentTarget.style.background = "#dc2626"; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "#ef4444"; }}
          >
            <Icon d={ic.trash} size={12} />
            Delete
          </button>
        </div>
      </div>
    </div>
  );
};
