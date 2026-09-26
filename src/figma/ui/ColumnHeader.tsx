// Column header shared by every table: resize (pointer or keyboard), reorder,
// hide, sort and auto-fit.
//
// The drag follows TanStack Table's columnResizingFeature (MIT), cloned for
// reference in clone_reference/tanstack-table: the size at pointer-down is
// captured once and every move applies the total delta to it. Moves are
// painted through `layout.preview` (a CSS variable write, no React render) and
// committed to state once, on release.
import React, { useRef, useState } from "react";
import {
  canHide,
  canReorder,
  canResize,
  columnSize,
  moveColumn,
  setColumnSize,
  toggleColumn,
  DEFAULT_MAX_SIZE,
  DEFAULT_MIN_SIZE,
  type ColumnDef,
} from "../../lib/columnLayout";
import { ActionMenu, isMenuKey, menuAnchor } from "./ActionMenu";
import type { ColumnLayout } from "./useColumnLayout";

export type { ColumnDef } from "../../lib/columnLayout";
export type { ColumnLayout } from "./useColumnLayout";
export { useColumnLayout } from "./useColumnLayout";
export { ColumnCells, TableScroll } from "./DataTable";

/** Keyboard resize steps, in px. */
const KEY_STEP = 8;
const KEY_STEP_LARGE = 32;

const justify = (def: ColumnDef) =>
  def.align === "right" ? "flex-end" : def.align === "center" ? "center" : "flex-start";

export function ColumnHeader({
  layout,
  className = "",
  style,
  sortKey,
  sortDir,
  onSort,
  leading,
}: {
  layout: ColumnLayout;
  className?: string;
  style?: React.CSSProperties;
  sortKey?: string | null;
  sortDir?: "asc" | "desc";
  onSort?: (key: string) => void;
  /** Content for a locked leading column's header cell, e.g. a select-all checkbox. */
  leading?: Partial<Record<string, React.ReactNode>>;
}) {
  const { defs, state, columns, setState, preview, reset, fitColumn, fitAll } = layout;
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [resizingId, setResizingId] = useState<string | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const [dropBefore, setDropBefore] = useState<string | null>(null);
  // Drag bookkeeping also lives in refs: a drag reports its first position
  // before React has re-rendered with the new state.
  const dragIdRef = useRef<string | null>(null);
  const dropBeforeRef = useRef<string | null>(null);
  const headerRef = useRef<HTMLDivElement | null>(null);

  const startResize = (event: React.PointerEvent, def: ColumnDef) => {
    if (!canResize(def) || event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const cell = (event.currentTarget as HTMLElement).closest("[data-header-cell]") as HTMLElement | null;
    const startX = event.clientX;
    // The shell can be zoomed, so pointer pixels and layout pixels differ.
    const rendered = cell?.getBoundingClientRect().width ?? 0;
    const layoutWidth = cell?.offsetWidth || rendered || columnSize(def, state);
    const scale = rendered > 0 && layoutWidth > 0 ? rendered / layoutWidth : 1;
    const startSize = columnSize(def, state);
    const base = state;
    let latest = base;
    let frame: number | null = null;
    let latestX = startX;

    const paint = () => {
      frame = null;
      latest = setColumnSize(defs, base, def.id, startSize + (latestX - startX) / (scale || 1));
      preview(latest);
    };
    const onMove = (move: PointerEvent) => {
      latestX = move.clientX;
      if (frame === null) frame = requestAnimationFrame(paint);
    };
    const onUp = () => {
      if (frame !== null) cancelAnimationFrame(frame);
      paint();
      setState(latest);
      setResizingId(null);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      document.body.classList.remove("col-resizing");
    };

    setResizingId(def.id);
    document.body.classList.add("col-resizing");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  };

  const resizeByKey = (event: React.KeyboardEvent, def: ColumnDef) => {
    const step = event.shiftKey ? KEY_STEP_LARGE : KEY_STEP;
    const current = columnSize(def, state);
    const target =
      event.key === "ArrowLeft" ? current - step
      : event.key === "ArrowRight" ? current + step
      : event.key === "Home" ? def.minSize ?? DEFAULT_MIN_SIZE
      : event.key === "End" ? def.maxSize ?? DEFAULT_MAX_SIZE
      : null;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      fitColumn(def.id);
      return;
    }
    if (target === null) return;
    event.preventDefault();
    setState((s) => setColumnSize(defs, s, def.id, target));
  };

  const onDragOverCell = (event: React.DragEvent, id: string) => {
    const active = dragIdRef.current;
    if (!active || active === id) return;
    const target = defs.find((def) => def.id === id);
    if (!target || !canReorder(target)) return;
    event.preventDefault();
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    const after = event.clientX > rect.left + rect.width / 2;
    const index = columns.findIndex((def) => def.id === id);
    const next = after ? columns[index + 1]?.id ?? null : id;
    dropBeforeRef.current = next;
    setDropBefore(next);
  };

  const finishDrag = () => {
    const active = dragIdRef.current;
    if (active) setState((current) => moveColumn(defs, current, active, dropBeforeRef.current));
    dragIdRef.current = null;
    dropBeforeRef.current = null;
    setDragId(null);
    setDropBefore(null);
  };

  const openMenuAt = (x: number, y: number) => setMenu({ x, y });

  return (
    <>
      <div
        ref={headerRef}
        className={`dl-col-header ${className}`}
        style={style}
        // Header clicks must not reach the list's "click empty space to clear selection".
        onClick={(event) => event.stopPropagation()}
        onContextMenu={(event) => {
          event.preventDefault();
          openMenuAt(event.clientX, event.clientY);
        }}
        onKeyDown={(event) => {
          if (isMenuKey(event) && headerRef.current) {
            event.preventDefault();
            const at = menuAnchor(headerRef.current);
            openMenuAt(at.x, at.y);
          }
        }}
        role="row"
      >
        {columns.map((def) => {
          const sortable = Boolean(def.sortKey && onSort);
          const active = sortable && sortKey === def.sortKey;
          const resizable = canResize(def);
          const size = columnSize(def, state);
          return (
            <div
              key={def.id}
              data-header-cell={def.id}
              className={`dl-col-head${dragId === def.id ? " dragging" : ""}${dropBefore === def.id ? " drop-target" : ""}${resizingId === def.id ? " resizing" : ""}`}
              draggable={canReorder(def)}
              onDragStart={(event) => {
                if (!canReorder(def)) return;
                dragIdRef.current = def.id;
                setDragId(def.id);
                event.dataTransfer.effectAllowed = "move";
                // Firefox needs data for a drag to start at all.
                event.dataTransfer.setData("text/plain", def.id);
              }}
              onDragOver={(event) => onDragOverCell(event, def.id)}
              onDrop={(event) => {
                event.preventDefault();
                finishDrag();
              }}
              onDragEnd={finishDrag}
              title={def.description || def.title || undefined}
              role="columnheader"
              aria-label={def.title || def.description}
              aria-sort={active ? (sortDir === "asc" ? "ascending" : "descending") : sortable ? "none" : undefined}
            >
              {leading?.[def.id] ?? (sortable ? (
                <button
                  type="button"
                  className="dl-col-head-label sortable"
                  style={{ justifyContent: justify(def) }}
                  onClick={() => onSort?.(def.sortKey!)}
                  title={`Sort by ${def.title}`}
                >
                  {def.title}
                  {active && <span className="dl-col-sort" aria-hidden="true">{sortDir === "asc" ? "▲" : "▼"}</span>}
                </button>
              ) : (
                <span className="dl-col-head-label" style={{ justifyContent: justify(def) }}>{def.title}</span>
              ))}
              {resizable && (
                <span
                  className="dl-col-grip"
                  tabIndex={0}
                  onPointerDown={(event) => startResize(event, def)}
                  onDoubleClick={(event) => {
                    event.stopPropagation();
                    fitColumn(def.id);
                  }}
                  onKeyDown={(event) => resizeByKey(event, def)}
                  title={`Drag or use ←/→ to resize ${def.title} · double-click or Enter to fit`}
                  role="separator"
                  aria-orientation="vertical"
                  aria-label={`Resize ${def.title}`}
                  aria-valuenow={size}
                  aria-valuemin={def.minSize ?? DEFAULT_MIN_SIZE}
                  aria-valuemax={def.maxSize ?? DEFAULT_MAX_SIZE}
                />
              )}
            </div>
          );
        })}
      </div>

      {menu && (
        <ActionMenu
          x={menu.x}
          y={menu.y}
          label="Columns"
          className="column-menu"
          onClose={() => setMenu(null)}
          items={[
            { heading: "Columns" },
            ...defs.filter((def) => canHide(def) && def.title).map((def) => ({
              label: def.title,
              checked: !state.hidden.includes(def.id),
              keepOpen: true,
              onSelect: () => setState((current) => toggleColumn(defs, current, def.id)),
            })),
            "-",
            { label: "Fit columns to content", onSelect: fitAll },
            { label: "Reset columns", onSelect: reset, disabled: layout.isDefault },
          ]}
        />
      )}
    </>
  );
}
