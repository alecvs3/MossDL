// Display metadata for engine file categories (engine/file_classifier.py).
// The engine decides what a file is; this only says how each category looks.

/** `icon` is a key of `ic` in figma/icons.tsx (kept as a name so this file stays UI-free). */
export interface KindMeta { label: string; color: string; icon: string }

export const KINDS: Record<string, KindMeta> = {
  video: { label: "Video", color: "#60a5fa", icon: "kindVideo" },
  audio: { label: "Audio", color: "#34d399", icon: "kindAudio" },
  pictures: { label: "Pictures", color: "#fbbf24", icon: "kindImage" },
  documents: { label: "Documents", color: "#f87171", icon: "kindDocument" },
  ebooks: { label: "eBooks & comics", color: "#fca5a5", icon: "kindBook" },
  archives: { label: "Archives", color: "#fb923c", icon: "kindArchive" },
  disk_images: { label: "Disk images", color: "#fdba74", icon: "kindDisc" },
  applications: { label: "Applications", color: "#f472b6", icon: "kindProgram" },
  code_data: { label: "Code & data", color: "#94a3b8", icon: "kindCode" },
  subtitles: { label: "Subtitles", color: "#a78bfa", icon: "kindSubtitle" },
  fonts: { label: "Fonts", color: "#c4b5fd", icon: "kindFont" },
  models_3d: { label: "3D models", color: "#5eead4", icon: "kindCube" },
  torrents: { label: "Torrents", color: "#86efac", icon: "kindMagnet" },
  checksums: { label: "Checksums & info", color: "#a3a3a3", icon: "kindHash" },
  other: { label: "Other files", color: "#a3a3a3", icon: "kindFile" },
};

/** The engine splits "audio" (by MIME) from "music" (by extension); people don't. */
export function displayKind(category: string | null | undefined): string {
  if (!category) return "other";
  if (category === "music") return "audio";
  return category in KINDS ? category : "other";
}

export function extensionOf(name: string): string {
  const leaf = name.split(/[?#]/)[0].split("/").pop() ?? "";
  const match = /\.([a-z0-9]{1,8})$/i.exec(leaf);
  return match ? match[1].toLowerCase() : "";
}
