import React from "react";

export function Toggle({
  on,
  onToggle,
  disabled = false,
}: {
  on: boolean;
  onToggle: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      onClick={disabled ? undefined : onToggle}
      role="switch"
      aria-checked={on}
      className={`win-toggle ${on ? "on" : ""}`}
      style={{
        background: on
          ? disabled
            ? "rgba(0,120,212,0.4)"
            : "var(--accent)"
          : "var(--surface-12)",
        opacity: disabled ? 0.45 : 1,
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    />
  );
}

export function SettingRow({
  label,
  sub,
  children,
  indent = false,
}: {
  label: string;
  sub?: string;
  children: React.ReactNode;
  indent?: boolean;
}) {
  return (
    <div
      className="flex items-center justify-between py-2.5"
      style={{
        borderBottom: "1px solid var(--line-05)",
        paddingLeft: indent ? "12px" : 0,
      }}
    >
      <div>
        <p style={{ fontSize: "11.5px", fontWeight: 500, color: "var(--ink-80)" }}>
          {label}
        </p>
        {sub && (
          <p
            style={{
              fontSize: "9.5px",
              color: "var(--ink-30)",
              marginTop: "1px",
              lineHeight: 1.4,
            }}
          >
            {sub}
          </p>
        )}
      </div>
      <div className="ml-6 shrink-0">{children}</div>
    </div>
  );
}

export function SectionLabel({ title }: { title: string }) {
  return (
    <p
      style={{
        fontSize: "10.5px",
        fontWeight: 700,
        letterSpacing: "0.08em",
        textTransform: "uppercase",
        color: "var(--ink-25)",
        marginTop: "18px",
        marginBottom: "4px",
      }}
    >
      {title}
    </p>
  );
}
