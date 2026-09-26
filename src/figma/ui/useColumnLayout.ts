// Column layout state for the app's tables: sizing, order, visibility and
// auto-fit, persisted per table. Rendering lives in ColumnHeader.tsx and
// DataTable.tsx; the pure rules live in lib/columnLayout.ts.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  autoFitAll,
  autoFitColumn,
  canResize,
  defaultState,
  fitToWidth,
  gridTemplate,
  isDefaultState,
  loadColumnState,
  minTableWidth,
  saveColumnState,
  visibleColumns,
  type ColumnDef,
  type ColumnState,
} from "../../lib/columnLayout";

export type { ColumnDef, ColumnState } from "../../lib/columnLayout";

/** Extra room so an auto-fitted column doesn't clip its own text. */
const FIT_PADDING = 18;

export interface ColumnLayout {
  defs: ColumnDef[];
  state: ColumnState;
  /** Visible columns in display order, computed once per layout change. */
  columns: ColumnDef[];
  template: string;
  minWidth: number;
  /** The table's scroll container; it carries the grid template for every row. */
  containerRef: React.RefObject<HTMLDivElement | null>;
  isDefault: boolean;
  setState: React.Dispatch<React.SetStateAction<ColumnState>>;
  /**
   * Paints a layout straight onto the table without a React render, so a
   * resize drag costs one style write per frame instead of re-rendering every
   * row. Pass null to repaint the committed layout.
   */
  preview: (state: ColumnState | null) => void;
  reset: () => void;
  fitColumn: (id: string) => void;
  fitAll: () => void;
}

/** CSS custom properties that size the header and every row of a table. */
export function tableVars(template: string, minWidth: number): Record<string, string> {
  return { "--dt-cols": template, "--dt-min": `${minWidth}px` };
}

/**
 * Measures the widest rendered cell of a column. Cells opt in with
 * `data-col="<id>"`, which every row does, including package parts.
 */
function measureColumn(container: HTMLElement | null, id: string): number | null {
  if (!container) return null;
  const cells = container.querySelectorAll<HTMLElement>(`[data-col="${CSS.escape(id)}"]`);
  let widest = 0;
  cells.forEach((cell) => {
    const content = cell.firstElementChild as HTMLElement | null;
    widest = Math.max(widest, (content ?? cell).scrollWidth);
  });
  return widest > 0 ? widest + FIT_PADDING : null;
}

export function useColumnLayout(storageKey: string, defs: ColumnDef[]): ColumnLayout {
  const [state, setState] = useState<ColumnState>(() => loadColumnState(storageKey, defs));
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fittedRef = useRef(false);

  useEffect(() => saveColumnState(storageKey, state), [storageKey, state]);

  // First paint: shrink oversized defaults so everything fits. Once only, so a
  // layout the user arranged is never rewritten.
  useEffect(() => {
    if (fittedRef.current) return;
    const width = containerRef.current?.clientWidth ?? 0;
    if (!width) return;
    fittedRef.current = true;
    setState((current) => fitToWidth(defs, current, width));
  }, [defs]);

  const template = useMemo(() => gridTemplate(defs, state), [defs, state]);
  const minWidth = useMemo(() => minTableWidth(defs, state), [defs, state]);

  const preview = useCallback((next: ColumnState | null) => {
    const el = containerRef.current;
    if (!el) return;
    const vars = next ? tableVars(gridTemplate(defs, next), minTableWidth(defs, next)) : tableVars(template, minWidth);
    for (const [key, value] of Object.entries(vars)) el.style.setProperty(key, value);
  }, [defs, template, minWidth]);

  const fitColumn = useCallback((id: string) => {
    const measured = measureColumn(containerRef.current, id);
    if (measured) setState((current) => autoFitColumn(defs, current, id, measured));
  }, [defs]);

  const fitAll = useCallback(() => {
    const measured: Record<string, number> = {};
    for (const def of defs) {
      if (!canResize(def)) continue;
      const value = measureColumn(containerRef.current, def.id);
      if (value) measured[def.id] = value;
    }
    setState((current) => autoFitAll(defs, current, measured));
  }, [defs]);

  const reset = useCallback(() => {
    fittedRef.current = false;
    setState(defaultState(defs));
  }, [defs]);

  return {
    defs,
    state,
    columns: useMemo(() => visibleColumns(defs, state), [defs, state]),
    template,
    minWidth,
    containerRef,
    isDefault: useMemo(() => isDefaultState(defs, state), [defs, state]),
    setState,
    preview,
    reset,
    fitColumn,
    fitAll,
  };
}
