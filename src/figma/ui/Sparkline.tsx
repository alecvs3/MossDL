import React from "react";

export function Sparkline({ history, color }: { history: number[]; color: string }) {
  if (history.length < 2) {
    return (
      <span style={{ fontSize: "9px", color, fontFamily: "'JetBrains Mono',monospace" }}>
        {(history[history.length - 1] ?? 0).toFixed(1)}
      </span>
    );
  }
  const max = Math.max(...history, 0.1);
  const W = 44;
  const H = 16;
  const pts = history
    .map((v, i) => `${(i / (history.length - 1)) * W},${H - (v / max) * (H - 2)}`)
    .join(" ");
  const lastX = W;
  const lastY = H - (history[history.length - 1] / max) * (H - 2);

  return (
    <div className="flex items-center gap-1.5 shrink-0">
      <svg width={W} height={H} style={{ overflow: "visible" }}>
        <polyline
          points={pts}
          fill="none"
          style={{ stroke: color }}
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
          opacity="0.65"
        />
        <circle cx={lastX} cy={lastY} r="2" opacity="0.9" style={{ fill: color }} />
      </svg>
      <span
        style={{
          fontSize: "9px",
          color,
          fontFamily: "'JetBrains Mono',monospace",
          minWidth: "30px",
        }}
      >
        {history[history.length - 1].toFixed(1)}
        <span style={{ fontSize: "7.5px", opacity: 0.55 }}>MB/s</span>
      </span>
    </div>
  );
}
