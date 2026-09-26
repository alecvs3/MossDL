// Right-click menus for Explore rows. Each entry calls back into the page,
// which does the engine work; this file only decides what makes sense where.
import { ic } from "../../icons";
import type { MenuEntry } from "../../ui/ActionMenu";
import { KINDS } from "../../../lib/fileKinds";
import type { ExploreNode } from "./exploreModel";
import { isDownloadable, type CheckState } from "./exploreView";

export interface MenuActions {
  download: (nodes: ExploreNode[], opts?: { pickFolder?: boolean }) => void;
  follow: (n: ExploreNode, headless?: boolean) => void;
  reexplore: (n: ExploreNode, headless: boolean) => void;
  copy: (text: string, what: string) => void;
  openExternal: (url: string) => void;
  setChecked: (nodes: ExploreNode[], on: boolean) => void;
  selectWhere: (pred: (n: ExploreNode) => boolean) => void;
  filterKind: (kind: string) => void;
  filterHost: (host: string) => void;
  toggleOpen: (n: ExploreNode, open?: boolean) => void;
  openAll: (n: ExploreNode, open: boolean) => void;
  remove: (nodes: ExploreNode[]) => void;
  /** Stream captures (HLS/DASH/media): pick a quality and merge. */
  media: (n: ExploreNode) => void;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function exploreMenu(n: ExploreNode, ctx: {
  actions: MenuActions;
  open: boolean;
  check: CheckState;
  files: ExploreNode[];
  selection: ExploreNode[];
  queued: (n: ExploreNode) => boolean;
  mediaEligible: (n: ExploreNode) => boolean;
}): MenuEntry[] {
  const { actions: a, open, check, files, selection } = ctx;

  // Right-clicking inside a multi-row selection acts on the whole selection.
  if (selection.length > 1 && selection.some((s) => s.id === n.id)) {
    return [
      { heading: plural(selection.length, "selected file") },
      { label: `Add ${plural(selection.length, "file")} to Downloads`, icon: ic.download, kbd: "Enter", onSelect: () => a.download(selection) },
      { label: "Download to folder…", icon: ic.folder, onSelect: () => a.download(selection, { pickFolder: true }) },
      "-",
      { label: `Copy ${plural(selection.length, "address")}`, icon: ic.copy, kbd: "Ctrl+C", onSelect: () => a.copy(selection.map((s) => s.url).join("\n"), "addresses") },
      { label: "Copy file names", onSelect: () => a.copy(selection.map((s) => s.label).join("\n"), "file names") },
      "-",
      { label: "Clear selection", kbd: "Esc", onSelect: () => a.setChecked(selection, false) },
      { label: `Remove ${plural(selection.length, "row")} from Explore`, icon: ic.trash, kbd: "Del", danger: true, onSelect: () => a.remove(selection) },
    ];
  }

  const expand: MenuEntry[] = n.children.length ? [
    { label: open ? "Collapse" : "Expand", icon: open ? ic.collapse : ic.expand, kbd: open ? "←" : "→", onSelect: () => a.toggleOpen(n) },
    { label: "Expand everything below", kbd: "*", onSelect: () => a.openAll(n, true) },
  ] : [];
  const select: MenuEntry = { label: check === "on" ? "Unselect" : "Select", kbd: "Space", onSelect: () => a.setChecked([n], check !== "on") };

  if (isDownloadable(n)) {
    const kind = n.kind && KINDS[n.kind] ? KINDS[n.kind] : null;
    return [
      { label: ctx.queued(n) ? "Add to Downloads again" : "Add to Downloads", icon: ic.download, kbd: "Enter", onSelect: () => a.download([n]) },
      { label: "Download to folder…", icon: ic.folder, onSelect: () => a.download([n], { pickFolder: true }) },
      ...(ctx.mediaEligible(n) ? [{ label: "Choose quality and merge streams…", icon: ic.layers, onSelect: () => a.media(n) }] : []),
      "-",
      select,
      ...(kind ? [
        { label: `Select all ${kind.label.toLowerCase()}`, onSelect: () => a.selectWhere((x) => x.kind === n.kind) },
        { label: `Show only ${kind.label.toLowerCase()}`, icon: ic.sliders, onSelect: () => a.filterKind(n.kind!) },
      ] : []),
      ...(n.host ? [
        { label: `Select everything from ${n.host}`, onSelect: () => a.selectWhere((x) => x.host === n.host) },
        { label: `Show ${n.host} in the filter`, icon: ic.sliders, onSelect: () => a.filterHost(n.host!) },
      ] : []),
      "-",
      { label: "Copy address", icon: ic.copy, kbd: "Ctrl+C", onSelect: () => a.copy(n.url!, "address") },
      { label: "Copy file name", onSelect: () => a.copy(n.label, "file name") },
      { label: "Open in browser", icon: ic.externalLink, onSelect: () => a.openExternal(n.url!) },
      "-",
      { label: "Remove from Explore", icon: ic.trash, kbd: "Del", danger: true, onSelect: () => a.remove([n]) },
    ];
  }

  if (n.type === "source" || n.type === "site") {
    const best = files.find((f) => f.best) ?? files[0];
    return [
      { label: "Download best match", icon: ic.download, disabled: !best, onSelect: () => best && a.download([best]) },
      { label: `Download all ${plural(files.length, "file")}`, disabled: !files.length, onSelect: () => a.download(files) },
      "-",
      ...expand,
      { label: "Collapse everything below", onSelect: () => a.openAll(n, false) },
      select,
      "-",
      ...(n.origin.kind === "crawl" ? [
        { label: "Explore again", icon: ic.refreshCw, onSelect: () => a.reexplore(n, false) },
        { label: "Explore again in browser", icon: ic.globe, onSelect: () => a.reexplore(n, true) },
      ] : []),
      ...(n.url ? [
        { label: "Open page in browser", icon: ic.externalLink, onSelect: () => a.openExternal(n.url!) },
        { label: "Copy page address", icon: ic.copy, onSelect: () => a.copy(n.url!, "page address") },
      ] : []),
      "-",
      { label: "Remove from Explore", icon: ic.trash, kbd: "Del", danger: true, onSelect: () => a.remove([n]) },
    ];
  }

  if (n.type === "group") {
    return [
      ...expand,
      "-",
      { label: `Select all in “${n.label}”`, kbd: "Space", onSelect: () => a.setChecked([n], true) },
      { label: `Download all ${plural(files.length, "file")}`, icon: ic.download, disabled: !files.length, onSelect: () => a.download(files) },
      { label: "Copy all addresses", icon: ic.copy, disabled: !files.length, onSelect: () => a.copy(files.map((f) => f.url).join("\n"), "addresses") },
    ];
  }

  // Buttons, steps, mirrors and folders.
  return [
    ...(n.followable ? [
      { label: n.children.length ? "Follow again" : "Follow", icon: ic.play, kbd: "Enter", onSelect: () => a.follow(n) },
      { label: "Follow in browser", icon: ic.globe, onSelect: () => a.follow(n, true) },
    ] : []),
    ...(files.length ? [{ label: `Download all ${plural(files.length, "file")}`, icon: ic.download, onSelect: () => a.download(files) }] : []),
    ...expand,
    select,
    "-",
    ...(n.url ? [
      { label: "Copy address", icon: ic.copy, kbd: "Ctrl+C", onSelect: () => a.copy(n.url!, "address") },
      { label: "Open in browser", icon: ic.externalLink, onSelect: () => a.openExternal(n.url!) },
    ] : []),
    { label: "Remove from Explore", icon: ic.trash, kbd: "Del", danger: true, onSelect: () => a.remove([n]) },
  ];
}
