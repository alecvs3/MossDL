// A small rounded tile for a file's type: the category's glyph in its colour
// on a faint wash of the same colour. Muted so rows stay calm.
import React from "react";
import { Icon, ic } from "../icons";
import { KINDS, displayKind } from "../../lib/fileKinds";

export function KindTile({ category, size = 18 }: { category?: string | null; size?: number }) {
  const kind = KINDS[displayKind(category)];
  return (
    <span
      className="kind-tile"
      style={{ width: size, height: size, color: kind.color, background: `color-mix(in srgb, ${kind.color} 16%, transparent)` }}
      title={kind.label}
      aria-hidden="true"
    >
      <Icon d={(ic as Record<string, string>)[kind.icon]} size={Math.round(size * 0.62)} />
    </span>
  );
}

/** A site's favicon on a neutral tile, the header counterpart of KindTile. */
export function SiteTile({ children, size = 22 }: { children: React.ReactNode; size?: number }) {
  return <span className="site-tile" style={{ width: size, height: size }} aria-hidden="true">{children}</span>;
}
