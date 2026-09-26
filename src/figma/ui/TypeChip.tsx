import React from "react";

export const typeColors: Record<string, string> = {
  // installers / executables — warm orange/red tones
  EXE: "rgba(251,146,60,0.55)",
  MSI: "rgba(252,165,86,0.5)",
  DMG: "rgba(251,146,60,0.42)",
  PKG: "rgba(251,146,60,0.42)",
  // archives — amber/yellow
  ZIP: "rgba(251,191,36,0.5)",
  RAR: "rgba(251,191,36,0.48)",
  "7Z": "rgba(251,191,36,0.45)",
  TAR: "rgba(251,191,36,0.45)",
  GZ:  "rgba(251,191,36,0.42)",
  // disk images — muted violet
  ISO: "rgba(167,139,250,0.5)",
  IMG: "rgba(167,139,250,0.45)",
  // documents
  PDF: "rgba(248,113,113,0.5)",
  // video
  MP4: "rgba(244,114,182,0.48)",
  MKV: "rgba(244,114,182,0.45)",
  AVI: "rgba(244,114,182,0.42)",
  MOV: "rgba(244,114,182,0.42)",
  // audio
  M4A: "rgba(34,211,238,0.48)",
  MP3: "rgba(34,211,238,0.45)",
  FLAC:"rgba(34,211,238,0.45)",
  // game / steam
  VPK: "rgba(74,222,128,0.48)",
  VPF: "rgba(45,212,191,0.45)",
};

export const fallbackTypeColor = "rgba(148,163,184,0.4)";

export function TypeChip({ type }: { type: string }) {
  const color = typeColors[type] ?? fallbackTypeColor;
  return (
    <span
      style={{
        color,
        fontSize: "8px",
        fontWeight: 700,
        letterSpacing: "0.07em",
        flexShrink: 0,
        lineHeight: 1,
        userSelect: "none",
        display: "inline-block",
        width: "28px",
        textAlign: "center",
      }}
    >
      {type}
    </span>
  );
}
