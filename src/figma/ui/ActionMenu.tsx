// Keyboard-first context menu used by Explore, Connections and the column
// header. Items are data; the menu owns focus, positioning and dismissal.
//
// Keys: ↑/↓/Home/End move, Enter/Space run, Esc/Tab close, a letter jumps to
// the next item starting with it. Focus returns to where it was on close.
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Icon, ic } from "../icons";

export type MenuAction = {
  label: string;
  icon?: string;
  kbd?: string;
  onSelect: () => void;
  disabled?: boolean;
  danger?: boolean;
  /** Present for toggles: renders a check and role="menuitemcheckbox". */
  checked?: boolean;
  /** Keep the menu open after selecting (for checkbox lists). */
  keepOpen?: boolean;
};
export type MenuEntry = MenuAction | "-" | { heading: string };

const isAction = (entry: MenuEntry): entry is MenuAction => typeof entry === "object" && "onSelect" in entry;

/** Anchor point for opening a menu from the keyboard (Menu key / Shift+F10). */
export function menuAnchor(el: Element): { x: number; y: number } {
  const rect = el.getBoundingClientRect();
  return { x: rect.left + 32, y: rect.bottom - 2 };
}
export const isMenuKey = (event: React.KeyboardEvent | KeyboardEvent) =>
  event.key === "ContextMenu" || (event.shiftKey && event.key === "F10");

export function ActionMenu({ x, y, items, label, onClose, className = "" }: {
  x: number;
  y: number;
  items: MenuEntry[];
  label: string;
  onClose: () => void;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const returnFocus = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null);
  const [pos, setPos] = useState({ left: x, top: y });

  // Keep the whole menu on screen: flip up when it would run off the bottom.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    setPos({
      left: Math.max(6, Math.min(x, window.innerWidth - width - 6)),
      top: y + height > window.innerHeight - 6 ? Math.max(6, y - height) : y,
    });
    el.querySelector<HTMLElement>("[data-menu-item]:not([aria-disabled=true])")?.focus();
  }, [x, y]);

  useEffect(() => {
    const target = returnFocus.current;
    const outside = (event: MouseEvent) => { if (!ref.current?.contains(event.target as Node)) onClose(); };
    window.addEventListener("mousedown", outside, true);
    window.addEventListener("resize", onClose);
    window.addEventListener("blur", onClose);
    return () => {
      window.removeEventListener("mousedown", outside, true);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("blur", onClose);
      if (target?.isConnected) target.focus({ preventScroll: true });
    };
  }, [onClose]);

  const run = (entry: MenuAction) => {
    if (entry.disabled) return;
    if (!entry.keepOpen) onClose();
    entry.onSelect();
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    const nodes = [...(ref.current?.querySelectorAll<HTMLElement>("[data-menu-item]:not([aria-disabled=true])") ?? [])];
    const index = nodes.indexOf(document.activeElement as HTMLElement);
    const move = (to: number) => { event.preventDefault(); nodes[(to + nodes.length) % nodes.length]?.focus(); };
    if (event.key === "ArrowDown") move(index + 1);
    else if (event.key === "ArrowUp") move(index - 1);
    else if (event.key === "Home") move(0);
    else if (event.key === "End") move(nodes.length - 1);
    else if (event.key === "Escape" || event.key === "Tab") { event.preventDefault(); onClose(); }
    else if (event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey && event.key !== " ") {
      const letter = event.key.toLowerCase();
      const order = [...nodes.slice(index + 1), ...nodes.slice(0, index + 1)];
      const hit = order.find((node) => node.dataset.label?.toLowerCase().startsWith(letter));
      if (hit) move(nodes.indexOf(hit));
    }
  };

  return (
    <div
      ref={ref}
      className={`context-menu action-menu ${className}`}
      style={pos}
      role="menu"
      aria-label={label}
      onKeyDown={onKeyDown}
      onContextMenu={(event) => event.preventDefault()}
    >
      {items.map((entry, i) => {
        if (entry === "-") return <div key={i} className="context-menu-sep" role="separator" />;
        if (!isAction(entry)) return <div key={i} className="action-menu-heading" aria-hidden="true">{entry.heading}</div>;
        const toggle = entry.checked !== undefined;
        return (
          <button
            key={i}
            type="button"
            data-menu-item
            data-label={entry.label}
            tabIndex={-1}
            role={toggle ? "menuitemcheckbox" : "menuitem"}
            aria-checked={toggle ? entry.checked : undefined}
            aria-disabled={entry.disabled || undefined}
            className={`context-menu-item${entry.danger ? " danger" : ""}`}
            onClick={() => run(entry)}
            onMouseMove={(event) => { if (!entry.disabled) event.currentTarget.focus(); }}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") { event.preventDefault(); run(entry); }
            }}
          >
            <span className="action-menu-icon" aria-hidden="true">
              {entry.checked ? <Icon d={ic.check} size={11} /> : entry.icon ? <Icon d={entry.icon} size={12} /> : null}
            </span>
            <span className="action-menu-label">{entry.label}</span>
            {entry.kbd && <span className="action-menu-kbd" aria-hidden="true">{entry.kbd}</span>}
          </button>
        );
      })}
    </div>
  );
}
