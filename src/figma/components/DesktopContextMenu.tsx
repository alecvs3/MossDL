import React, { useRef, useEffect } from "react";
import type { Page } from "../types";
import { Icon, ic } from "../icons";

export interface DesktopContextMenuProps {
  x: number;
  y: number;
  currentPage?: Page;
  onClose: () => void;
  onAddUrl: () => void;
  onPasteUrl?: () => void;
  onOpenFolder?: () => void;
  onSelectAll?: () => void;
  onClearCompleted?: () => void;
  onNavigatePage?: (page: Page) => void;
}

export function DesktopContextMenu({
  x,
  y,
  currentPage = "downloads",
  onClose,
  onAddUrl,
  onPasteUrl,
  onOpenFolder,
  onSelectAll,
  onClearCompleted,
  onNavigatePage,
}: DesktopContextMenuProps) {
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

  const menuW = 212;
  const menuH = 260;
  const cx = Math.min(x, window.innerWidth - menuW - 8);
  const cy = Math.min(y, window.innerHeight - menuH - 8);

  const act = (fn?: () => void) => {
    fn?.();
    onClose();
  };

  return (
    <div ref={menuRef} className="context-menu" style={{ left: cx, top: cy }}>
      <div className="context-menu-item" onClick={() => act(onAddUrl)}>
        <Icon d={ic.plus} size={12} style={{ color: "var(--accent-light)", flexShrink: 0 }} />
        <span style={{ color: "var(--accent-light)", fontWeight: 600 }}>Add Link…</span>
        <span className="menu-item-kbd" style={{ marginLeft: "auto" }}>Ctrl+N</span>
      </div>
      {onPasteUrl && (
        <div className="context-menu-item" onClick={() => act(onPasteUrl)}>
          <Icon d={ic.clipboardList} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
          <span>Paste URL</span>
          <span className="menu-item-kbd" style={{ marginLeft: "auto" }}>Ctrl+V</span>
        </div>
      )}
      {onOpenFolder && (
        <div className="context-menu-item" onClick={() => act(onOpenFolder)}>
          <Icon d={ic.folder} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
          <span>Open Download Folder</span>
        </div>
      )}
      {onNavigatePage && (
        <>
          <div className="context-menu-sep" />
          {currentPage !== "downloads" && (
            <div className="context-menu-item" onClick={() => act(() => onNavigatePage("downloads"))}>
              <Icon d={ic.download} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
              <span>Go to Downloads</span>
            </div>
          )}
          {currentPage !== "explore" && (
            <div className="context-menu-item" onClick={() => act(() => onNavigatePage("explore"))}>
              <Icon d={ic.globe} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
              <span>Open Explore</span>
            </div>
          )}
        </>
      )}
      {onSelectAll && (
        <>
          <div className="context-menu-sep" />
          <div className="context-menu-item" onClick={() => act(onSelectAll)}>
            <Icon d={ic.checkSquare} size={12} style={{ color: "var(--ink-45)", flexShrink: 0 }} />
            <span>Select All</span>
            <span className="menu-item-kbd" style={{ marginLeft: "auto" }}>Ctrl+A</span>
          </div>
        </>
      )}
      {onClearCompleted && (
        <>
          <div className="context-menu-sep" />
          <div className="context-menu-item danger" onClick={() => act(onClearCompleted)}>
            <Icon d={ic.trash} size={12} style={{ flexShrink: 0 }} />
            <span>Clear Completed</span>
          </div>
        </>
      )}
    </div>
  );
}
