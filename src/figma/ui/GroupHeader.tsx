import React from "react";
import { Icon, ic } from "../icons";

export function GroupHeader({
  icon,
  label,
  count,
  speed,
  collapsed,
  onToggle,
}: {
  icon: string;
  label: string;
  count: number;
  speed?: number;
  collapsed: boolean;
  onToggle: () => void;
}) {
  const accentColor =
    label === "Transferring"
      ? "var(--accent-light)"
      : label === "Failed"
      ? "var(--danger)"
      : "var(--ink-40)";

  return (
    <div className="dl-group-header" onClick={onToggle}>
      <Icon d={icon} size={11} style={{ color: accentColor }} />
      <span
        style={{
          fontSize: "9.5px",
          fontWeight: 700,
          color: accentColor,
          letterSpacing: "0.05em",
          textTransform: "uppercase",
        }}
      >
        {label}
      </span>
      <span
        style={{
          fontSize: "9px",
          color: "var(--ink-25)",
          fontFamily: "'JetBrains Mono',monospace",
        }}
      >
        {count}
      </span>
      {speed !== undefined && speed > 0 && (
        <span
          style={{
            fontSize: "9px",
            color: "var(--success)",
            fontFamily: "'JetBrains Mono',monospace",
          }}
        >
          ↓ {speed.toFixed(1)} MB/s
        </span>
      )}
      <div
        className="ml-auto transition-transform duration-200"
        style={{
          color: "var(--ink-20)",
          transform: collapsed ? "rotate(-90deg)" : "rotate(0deg)",
        }}
      >
        <Icon d={ic.chevronDown} size={11} />
      </div>
    </div>
  );
}
