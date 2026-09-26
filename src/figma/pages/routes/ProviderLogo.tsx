// App-icon style provider tile: rounded square, hairline outline, logo either
// edge to edge (brand tiles) or centred on a neutral surface (glyph logos).
import React from "react";
import { Icon, ic } from "../../icons";
import type { Provider } from "./providers";

export function ProviderLogo({ provider, size = 30, kind }: { provider?: Provider; size?: number; kind?: "direct" | "custom" | "import" }) {
  const style = { width: size, height: size, borderRadius: Math.round(size * 0.24) } as React.CSSProperties;
  if (!provider) {
    const icon = kind === "direct" ? ic.globe : kind === "import" ? ic.upload : ic.shield;
    return (
      <span className="app-tile glyph" style={style} aria-hidden="true">
        <Icon d={icon} size={Math.round(size * 0.5)} />
      </span>
    );
  }
  if (!provider.logo) {
    return (
      <span className="app-tile letter" style={{ ...style, background: provider.color, fontSize: size * 0.45 }} aria-hidden="true">
        {provider.name[0]}
      </span>
    );
  }
  return (
    <span className={`app-tile${provider.fullBleed ? " full" : " glyph"}`} style={style} aria-hidden="true">
      <img src={provider.logo} alt="" draggable={false} style={provider.fullBleed ? undefined : { width: "72%", height: "72%" }} />
    </span>
  );
}
