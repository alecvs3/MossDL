// Table body pieces shared by every list: one scroll container that owns the
// grid template, and the cell renderer every row uses.
//
// The header is rendered inside the same scroll container and pinned with
// `position: sticky`, so it always has exactly the rows' width: a vertical
// scrollbar can no longer push the rows out of line with their headers, and
// horizontal scrolling moves both together.
import React from "react";
import type { ColumnDef } from "../../lib/columnLayout";
import { tableVars, type ColumnLayout } from "./useColumnLayout";

const justify = (def: ColumnDef) =>
  def.align === "right" ? "flex-end" : def.align === "center" ? "center" : "flex-start";

type ScrollProps = Omit<React.HTMLAttributes<HTMLDivElement>, "children"> & {
  layout: ColumnLayout;
  /** The ColumnHeader; pinned to the top while the rows scroll. */
  header: React.ReactNode;
  children: React.ReactNode;
};

export function TableScroll({ layout, header, children, className = "", style, ...rest }: ScrollProps) {
  return (
    <div
      {...rest}
      ref={layout.containerRef}
      className={`dt-scroll ${className}`}
      style={{ ...tableVars(layout.template, layout.minWidth), ...style } as React.CSSProperties}
    >
      <div className="dt-content">
        {header}
        {children}
      </div>
    </div>
  );
}

/**
 * Renders a row's cells in the current column order, skipping hidden ones, so
 * the header and every row (including nested package parts) stay aligned.
 */
export function ColumnCells({
  layout,
  cells,
}: {
  layout: ColumnLayout;
  cells: Record<string, React.ReactNode>;
}) {
  return (
    <>
      {layout.columns.map((def) => (
        <div
          key={def.id}
          data-col={def.id}
          role="gridcell"
          className={`download-cell col-${def.id}`}
          style={{ justifyContent: justify(def), textAlign: def.align ?? "left" }}
        >
          {cells[def.id] ?? null}
        </div>
      ))}
    </>
  );
}
