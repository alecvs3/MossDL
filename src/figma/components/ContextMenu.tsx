import React, { useRef, useEffect } from "react";
import type { DownloadWithHistory } from "../types";
import { Icon, ic } from "../icons";

export function ContextMenu({
  x,
  y,
  d,
  onClose,
  onAction,
  packageCount,
}: {
  x: number;
  y: number;
  d: DownloadWithHistory;
  onClose: () => void;
  onAction: (action: string, id: string) => void;
  packageCount?: number;
}) {
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) onClose();
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", handleDown);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleDown);
      document.removeEventListener("keydown", handleKey);
    };
  }, [onClose]);

  // clamp position so menu doesn't overflow viewport
  const menuW = 204;
  const menuH = 240;
  const cx = Math.min(x, window.innerWidth - menuW - 8);
  const cy = Math.min(y, window.innerHeight - menuH - 8);

  const act = (action: string) => {
    onAction(action, d.id);
    onClose();
  };

  const isFolder = typeof packageCount === "number" && packageCount > 1;

  return (
    <div ref={menuRef} className="context-menu" style={{ left: cx, top: cy }}>
      <div className="context-menu-item" onClick={() => act("open")}>
        <Icon d={ic.externalLink} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
        <span>Open details</span>
      </div>
      <div className="context-menu-item" onClick={() => act("copy")}>
        <Icon d={ic.clipboardList} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
        <span>{isFolder ? `Copy All URLs (${packageCount})` : "Copy URL"}</span>
      </div>
      <div className="context-menu-item" onClick={() => act("directory")}>
        <Icon d={ic.folder} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
        <span>Set Save Directory…</span>
      </div>
      <div className="context-menu-item" onClick={() => act("folder")}>
        <Icon d={ic.folder} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
        <span>Open Download Folder</span>
      </div>
      {!isFolder && (
        <>
          <div className="context-menu-sep" />
          <div className="context-menu-item" onClick={() => act("rename")}>
            <Icon d={ic.pencil} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
            <span>Rename…</span>
          </div>
        </>
      )}
      <div className="context-menu-item" onClick={() => act("prioritize")}>
        <Icon d={ic.zap} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
        <span>{isFolder ? `Move All to Top (${packageCount})` : "Move to Top"}</span>
      </div>
      <div className="context-menu-item" onClick={() => act("toggle")}>
        <Icon
          d={d.status === "downloading" ? ic.pause : ic.play}
          size={12}
          style={{ color: "var(--ink-45)", flexShrink: 0 }}
        />
        <span>
          {isFolder
            ? d.status === "downloading"
              ? `Pause All Parts (${packageCount})`
              : `Resume All Parts (${packageCount})`
            : d.status === "downloading"
            ? "Pause"
            : "Resume"}
        </span>
      </div>
      <div className="context-menu-item" onClick={() => act("retry")}>
        <Icon d={ic.refreshCw} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
        <span>{isFolder ? `Retry All Parts (${packageCount})` : "Retry"}</span>
      </div>
      <div className="context-menu-sep" />
      <div className="context-menu-item danger" onClick={() => act("remove")}>
        <Icon d={ic.trash} size={12} style={{ flexShrink: 0 }} />
        <span>{isFolder ? `Remove All Parts (${packageCount})` : "Remove"}</span>
      </div>
    </div>
  );
}
