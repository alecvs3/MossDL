// Column model for the app's tables: sizing, ordering, visibility and auto-fit.
//
// The logic follows TanStack Table's column features (MIT), cloned for
// reference in clone_reference/tanstack-table:
//   packages/table-core/src/features/column-sizing/columnSizingFeature.utils.ts
//   packages/table-core/src/features/column-resizing/columnResizingFeature.utils.ts
//   packages/table-core/src/features/column-ordering/columnOrderingFeature.utils.ts
//
// The rules that matter, and that a hand-rolled version gets wrong:
//   * a size is always clamp(minSize, size, maxSize) — dragging never removes a
//     column, it stops at the minimum;
//   * a drag is measured from the size captured at pointer-down plus the total
//     delta, never from the live rendered width, so it cannot drift;
//   * saved order is reconciled against the current definitions: known ids in
//     saved order, unknown ids dropped, new ones appended in definition order.
//
// Rendering lives in figma/ui/ColumnHeader.tsx.

export type ColumnAlign = "left" | "center" | "right";

export interface ColumnDef {
  id: string;
  /** Header label. Empty for affordance columns such as selection. */
  title: string;
  /** Starting width in px. A flexible column ignores it until it is dragged. */
  size?: number;
  minSize?: number;
  maxSize?: number;
  /** Takes the leftover space; exactly one column should set this. */
  flex?: boolean;
  align?: ColumnAlign;
  /** Affordance columns: never resized, moved or hidden. */
  locked?: boolean;
  enableResizing?: boolean;
  enableHiding?: boolean;
  sortKey?: string;
  /** Available in the column menu, but off until the user turns it on. */
  hiddenByDefault?: boolean;
  description?: string;
}

export interface ColumnState {
  order: string[];
  /** Explicit sizes; columns without an entry use their definition size. */
  sizing: Record<string, number>;
  /** Only columns the user turned off; everything else is visible. */
  hidden: string[];
}

export const DEFAULT_COLUMN_SIZE = 150;
export const DEFAULT_MIN_SIZE = 32;
export const DEFAULT_MAX_SIZE = 1200;

const clone = (state: ColumnState): ColumnState => ({
  order: [...state.order],
  sizing: { ...state.sizing },
  hidden: [...state.hidden],
});

/** Flexible columns are resizable too: dragging one pins it to that width. */
export const canResize = (def: ColumnDef): boolean => !def.locked && (def.enableResizing ?? true);
/** True while a flexible column still absorbs the leftover space. */
export const isFlexing = (def: ColumnDef, state: ColumnState): boolean =>
  Boolean(def.flex) && !Object.prototype.hasOwnProperty.call(state.sizing, def.id);
export const canHide = (def: ColumnDef): boolean => !def.locked && (def.enableHiding ?? true);
export const canReorder = (def: ColumnDef): boolean => !def.locked;

export function columnSize(def: ColumnDef, state?: ColumnState): number {
  const stored = state && Object.prototype.hasOwnProperty.call(state.sizing, def.id)
    ? state.sizing[def.id]
    : undefined;
  return Math.min(
    Math.max(def.minSize ?? DEFAULT_MIN_SIZE, stored ?? def.size ?? DEFAULT_COLUMN_SIZE),
    def.maxSize ?? DEFAULT_MAX_SIZE
  );
}

export function defaultState(defs: ColumnDef[]): ColumnState {
  return {
    order: defs.map((def) => def.id),
    sizing: {},
    hidden: defs.filter((def) => def.hiddenByDefault && canHide(def)).map((def) => def.id),
  };
}

/**
 * Reconciles saved state with the current definitions so a stored layout keeps
 * working after columns are added, removed or renamed.
 */
export function normalizeState(defs: ColumnDef[], saved?: Partial<ColumnState> | null): ColumnState {
  const known = new Map(defs.map((def) => [def.id, def]));
  if (!saved) return defaultState(defs);

  const order: string[] = [];
  for (const id of saved.order ?? []) {
    if (known.has(id) && !order.includes(id)) order.push(id);
  }
  defs.forEach((def, index) => {
    if (order.includes(def.id)) return;
    const previous = defs[index - 1]?.id;
    const at = previous ? order.indexOf(previous) + 1 : 0;
    order.splice(at < 0 ? order.length : at, 0, def.id);
  });

  const sizing: Record<string, number> = {};
  for (const [id, value] of Object.entries(saved.sizing ?? {})) {
    const def = known.get(id);
    if (!def || !canResize(def) || !Number.isFinite(value)) continue;
    sizing[id] = columnSize(def, { order, sizing: { [id]: value }, hidden: [] });
  }

  const hidden = (saved.hidden ?? []).filter((id) => known.get(id) && canHide(known.get(id)!));
  const seen = new Set([...(saved.order ?? []), ...Object.keys(saved.sizing ?? {}), ...(saved.hidden ?? [])]);
  for (const def of defs) {
    // A column added since the save keeps its default visibility.
    if (def.hiddenByDefault && !seen.has(def.id) && !hidden.includes(def.id)) hidden.push(def.id);
  }
  return { order, sizing, hidden };
}

export function visibleColumns(defs: ColumnDef[], state: ColumnState): ColumnDef[] {
  const known = new Map(defs.map((def) => [def.id, def]));
  return state.order
    .map((id) => known.get(id))
    .filter((def): def is ColumnDef => Boolean(def) && !state.hidden.includes(def!.id));
}

/** CSS grid template for the header and every row, so they always line up. */
export function gridTemplate(defs: ColumnDef[], state: ColumnState): string {
  return visibleColumns(defs, state)
    .map((def) =>
      isFlexing(def, state) ? `minmax(${def.minSize ?? DEFAULT_MIN_SIZE}px, 1fr)` : `${columnSize(def, state)}px`
    )
    .join(" ");
}

export function minTableWidth(defs: ColumnDef[], state: ColumnState): number {
  return visibleColumns(defs, state).reduce(
    (total, def) => total + (isFlexing(def, state) ? def.minSize ?? DEFAULT_MIN_SIZE : columnSize(def, state)),
    0
  );
}

/** Sets a size, clamped to the column's bounds. Never hides the column. */
export function setColumnSize(defs: ColumnDef[], state: ColumnState, id: string, size: number): ColumnState {
  const def = defs.find((item) => item.id === id);
  if (!def || !canResize(def) || !Number.isFinite(size)) return state;
  // Sizing a flexible column pins it; "Reset columns" hands the slack back.
  const next = clone(state);
  next.sizing[id] = columnSize(def, { ...state, sizing: { ...state.sizing, [id]: Math.round(size) } });
  return next;
}

export function toggleColumn(defs: ColumnDef[], state: ColumnState, id: string): ColumnState {
  const def = defs.find((item) => item.id === id);
  if (!def || !canHide(def)) return state;
  const next = clone(state);
  if (next.hidden.includes(id)) {
    next.hidden = next.hidden.filter((item) => item !== id);
  } else if (visibleColumns(defs, state).filter((column) => !column.locked).length > 1) {
    // Never hide the last content column: that would leave an empty table.
    next.hidden.push(id);
  }
  return next;
}

/** Moves `id` so it lands before `beforeId` (or last when null). */
export function moveColumn(defs: ColumnDef[], state: ColumnState, id: string, beforeId: string | null): ColumnState {
  const def = defs.find((item) => item.id === id);
  if (!def || !canReorder(def)) return state;
  const target = beforeId ? defs.find((item) => item.id === beforeId) : null;
  if (beforeId && (!target || !canReorder(target))) return state;
  const next = clone(state);
  const from = next.order.indexOf(id);
  if (from === -1) return state;
  next.order.splice(from, 1);
  const at = beforeId ? next.order.indexOf(beforeId) : next.order.length;
  next.order.splice(at === -1 ? next.order.length : at, 0, id);
  return next;
}

export function autoFitColumn(defs: ColumnDef[], state: ColumnState, id: string, measured: number): ColumnState {
  return setColumnSize(defs, state, id, measured);
}

export function autoFitAll(defs: ColumnDef[], state: ColumnState, measured: Record<string, number>): ColumnState {
  let next = state;
  for (const [id, value] of Object.entries(measured)) next = autoFitColumn(defs, next, id, value);
  return next;
}

export function isDefaultState(defs: ColumnDef[], state: ColumnState): boolean {
  const base = defaultState(defs);
  if ([...state.hidden].sort().join() !== [...base.hidden].sort().join()) return false;
  if (state.order.join() !== base.order.join()) return false;
  return defs.every((def) => columnSize(def, state) === columnSize(def, base));
}

/** Shrinks oversized defaults so a narrow window shows every column. */
export function fitToWidth(defs: ColumnDef[], state: ColumnState, available: number): ColumnState {
  const columns = visibleColumns(defs, state);
  const fixed = columns.filter((def) => !isFlexing(def, state));
  const flexMin = columns
    .filter((def) => isFlexing(def, state))
    .reduce((total, def) => total + (def.minSize ?? DEFAULT_MIN_SIZE), 0);
  const fixedTotal = fixed.reduce((total, def) => total + columnSize(def, state), 0);
  const slack = available - fixedTotal - flexMin;
  if (slack >= 0 || !fixed.length) return state;

  const shrinkable = fixed.reduce((total, def) => total + (columnSize(def, state) - (def.minSize ?? DEFAULT_MIN_SIZE)), 0);
  if (shrinkable <= 0) return state;
  const factor = Math.max(0, 1 + slack / shrinkable);
  let next = state;
  for (const def of fixed) {
    const min = def.minSize ?? DEFAULT_MIN_SIZE;
    next = setColumnSize(defs, next, def.id, min + (columnSize(def, state) - min) * factor);
  }
  return next;
}

export function loadColumnState(key: string, defs: ColumnDef[]): ColumnState {
  try {
    const raw = typeof localStorage === "undefined" ? null : localStorage.getItem(key);
    return normalizeState(defs, raw ? (JSON.parse(raw) as Partial<ColumnState>) : null);
  } catch {
    return defaultState(defs);
  }
}

export function saveColumnState(key: string, state: ColumnState): void {
  try {
    if (typeof localStorage !== "undefined") localStorage.setItem(key, JSON.stringify(state));
  } catch {
    // A full or blocked storage quota must never break the table.
  }
}
