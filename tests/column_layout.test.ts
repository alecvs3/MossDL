// Behaviour ported from TanStack Table's column features; see
// clone_reference/tanstack-table and src/lib/columnLayout.ts.
import assert from "node:assert/strict";
import {
  autoFitColumn,
  canHide,
  canReorder,
  canResize,
  columnSize,
  defaultState,
  fitToWidth,
  gridTemplate,
  isDefaultState,
  isFlexing,
  minTableWidth,
  moveColumn,
  normalizeState,
  setColumnSize,
  toggleColumn,
  visibleColumns,
  type ColumnDef,
} from "../src/lib/columnLayout.ts";

const defs: ColumnDef[] = [
  { id: "select", title: "", size: 36, minSize: 36, maxSize: 36, locked: true },
  { id: "name", title: "File", minSize: 200, flex: true, sortKey: "name" },
  { id: "status", title: "Status", size: 104, minSize: 76, maxSize: 260 },
  { id: "speed", title: "Speed", size: 100, minSize: 70, maxSize: 220, sortKey: "speed" },
  { id: "size", title: "Size", size: 70, minSize: 52, maxSize: 180, sortKey: "size" },
  { id: "host", title: "Host", size: 110, minSize: 70, hiddenByDefault: true },
  { id: "actions", title: "", size: 64, minSize: 64, maxSize: 64, locked: true },
];

const base = defaultState(defs);
assert.equal(base.order.join(","), "select,name,status,speed,size,host,actions");
assert.deepEqual(base.hidden, ["host"], "optional columns start hidden");
assert.equal(gridTemplate(defs, base), "36px minmax(200px, 1fr) 104px 100px 70px 64px");
assert.equal(minTableWidth(defs, base), 36 + 200 + 104 + 100 + 70 + 64);

// ── what can be touched ───────────────────────────────────────────────────
// Affordance columns expose no resize grip; that stray divider beside "File"
// was a locked column advertising a resize it never performed.
assert.equal(canResize(defs[0]), false, "locked columns are not resizable");
assert.equal(canResize(defs[1]), true, "the flexible column can be pinned by dragging it");
assert.equal(canResize(defs[2]), true);
assert.equal(canHide(defs[0]), false);
assert.equal(canReorder(defs[6]), false);

// ── resize clamps, and never hides ────────────────────────────────────────
assert.equal(setColumnSize(defs, base, "status", 140).sizing.status, 140);
assert.equal(setColumnSize(defs, base, "status", 10).sizing.status, 76,
  "dragging far left stops at the minimum instead of removing the column");
assert.deepEqual(setColumnSize(defs, base, "status", 10).hidden, base.hidden,
  "resizing never changes which columns are shown");
assert.equal(setColumnSize(defs, base, "status", 9999).sizing.status, 260, "resize stops at the maximum");
assert.deepEqual(setColumnSize(defs, base, "select", 200), base, "locked columns ignore resizes");
// Dragging the flexible column pins it, exactly like the Name column in Explorer.
const pinned = setColumnSize(defs, base, "name", 500);
assert.equal(pinned.sizing.name, 500);
assert.equal(isFlexing(defs[1], base), true, "it absorbs the slack until it is dragged");
assert.equal(isFlexing(defs[1], pinned), false, "after dragging it keeps the width it was given");
assert.equal(gridTemplate(defs, pinned), "36px 500px 104px 100px 70px 64px");
assert.equal(minTableWidth(defs, pinned) > minTableWidth(defs, base), true,
  "a pinned wide column makes the table scroll instead of squeezing the rest");
assert.deepEqual(setColumnSize(defs, base, "status", Number.NaN), base, "a bad value is ignored");

// A drag applies the total delta to the size captured at pointer-down, so
// repeated moves land on an absolute size rather than compounding.
const startSize = columnSize(defs[2], base);
let dragged = base;
for (const delta of [10, 25, 60, 45]) {
  dragged = setColumnSize(defs, dragged, "status", startSize + delta);
}
assert.equal(dragged.sizing.status, startSize + 45, "the last position wins; deltas never accumulate");

// ── show / hide ───────────────────────────────────────────────────────────
const hidden = toggleColumn(defs, base, "speed");
assert.deepEqual(hidden.hidden.sort(), ["host", "speed"]);
assert.equal(visibleColumns(defs, hidden).some((def) => def.id === "speed"), false);
assert.equal(gridTemplate(defs, hidden), "36px minmax(200px, 1fr) 104px 70px 64px");
assert.equal(toggleColumn(defs, hidden, "speed").hidden.includes("speed"), false, "toggling again brings it back");
assert.equal(toggleColumn(defs, base, "host").hidden.includes("host"), false, "hidden optional columns can be shown");
assert.deepEqual(toggleColumn(defs, base, "select"), base, "locked columns never hide");
// The last content column stays: an empty table helps nobody.
let stripped = base;
for (const id of ["status", "speed", "size", "name"]) stripped = toggleColumn(defs, stripped, id);
assert.equal(visibleColumns(defs, stripped).filter((def) => !def.locked).length >= 1, true);

// ── reorder ───────────────────────────────────────────────────────────────
assert.equal(moveColumn(defs, base, "size", "status").order.join(","),
  "select,name,size,status,speed,host,actions");
assert.equal(moveColumn(defs, base, "status", null).order.join(","),
  "select,name,speed,size,host,actions,status", "a null target moves the column last");
assert.deepEqual(moveColumn(defs, base, "select", "name"), base, "locked columns never move");
assert.deepEqual(moveColumn(defs, base, "status", "actions"), base, "columns never move past a locked column");

// ── auto-fit ──────────────────────────────────────────────────────────────
assert.equal(autoFitColumn(defs, base, "size", 120).sizing.size, 120);
assert.equal(autoFitColumn(defs, base, "size", 12).sizing.size, 52, "auto-fit respects the minimum");

// ── saved layouts survive column changes ──────────────────────────────────
const saved = { order: ["name", "status", "gone", "select"], sizing: { status: 200, gone: 90 }, hidden: ["gone"] };
const normalized = normalizeState(defs, saved);
assert.equal(normalized.order.includes("gone"), false, "columns that no longer exist are dropped");
assert.equal(normalized.order.length, defs.length, "columns added since the save reappear");
assert.equal(normalized.sizing.status, 200, "saved sizes are kept");
assert.equal(normalized.sizing.gone, undefined);
assert.deepEqual(normalized.hidden, ["host"], "a newly added optional column keeps its default");
assert.equal(normalizeState(defs, { sizing: { status: 5 } }).sizing.status, 76, "saved sizes clamp to the minimum");
assert.deepEqual(normalizeState(defs, null), base);

const withoutSpeed = { order: ["select", "name", "status", "size", "actions"], sizing: {}, hidden: [] };
assert.equal(normalizeState(defs, withoutSpeed).order.join(","), "select,name,status,speed,size,host,actions",
  "a column added later slots in beside its neighbour");

// ── first-run fit ─────────────────────────────────────────────────────────
assert.deepEqual(fitToWidth(defs, base, 1400), base, "a wide table keeps its default sizes");
const squeezed = fitToWidth(defs, base, 480);
assert.equal(minTableWidth(defs, squeezed) <= minTableWidth(defs, base), true, "a narrow table shrinks to fit");
for (const def of defs) {
  if (!canResize(def)) continue;
  assert.equal(columnSize(def, squeezed) >= (def.minSize ?? 0), true, `${def.id} never shrinks past its minimum`);
}

assert.equal(isDefaultState(defs, base), true);
assert.equal(isDefaultState(defs, setColumnSize(defs, base, "status", 140)), false);

console.log("Column layout tests passed");
