// Column sets for the app's tables. Sizes follow TanStack Table's model:
// `size` is the starting width, `minSize`/`maxSize` bound every resize, and a
// dragged column clamps at its minimum instead of disappearing.
import type { ColumnDef } from "../../lib/columnLayout";

export const DOWNLOAD_COLUMNS_KEY = "nexload.columns.downloads.v3";

export const DOWNLOAD_COLUMNS: ColumnDef[] = [
  // Affordance columns are locked: never resized, moved or hidden.
  { id: "select", title: "", size: 36, minSize: 36, maxSize: 36, locked: true, description: "Select" },
  { id: "name", title: "File", minSize: 180, flex: true, sortKey: "name" },
  { id: "status", title: "Status", size: 118, minSize: 88, maxSize: 260 },
  { id: "host", title: "Host", size: 130, minSize: 70, maxSize: 260, description: "Site the download comes from" },
  { id: "speed", title: "Speed", size: 96, minSize: 70, maxSize: 220, sortKey: "speed" },
  { id: "eta", title: "ETA", size: 60, minSize: 44, maxSize: 160, align: "right" },
  { id: "pct", title: "Progress", size: 120, minSize: 92, maxSize: 240, description: "How much is downloaded" },
  { id: "size", title: "Size", size: 124, minSize: 100, maxSize: 220, align: "right", sortKey: "size" },
  { id: "type", title: "Type", size: 48, minSize: 40, maxSize: 120, align: "center", hiddenByDefault: true },
  { id: "added", title: "Added", size: 110, minSize: 70, maxSize: 220, hiddenByDefault: true, description: "When the download was added" },
  { id: "folder", title: "Saved to", size: 200, minSize: 110, maxSize: 600, hiddenByDefault: true, description: "Destination folder" },
  { id: "actions", title: "", size: 64, minSize: 64, maxSize: 64, align: "right", locked: true, description: "Row actions" },
];

export const EXPLORE_COLUMNS_KEY = "nexload.columns.explore.v2";

export const EXPLORE_COLUMNS: ColumnDef[] = [
  { id: "select", title: "", size: 30, minSize: 30, maxSize: 30, locked: true, description: "Select" },
  { id: "name", title: "Name", minSize: 200, flex: true, sortKey: "name" },
  { id: "kind", title: "Type", size: 58, minSize: 44, maxSize: 110, sortKey: "kind", description: "File type" },
  { id: "size", title: "Size", size: 76, minSize: 56, maxSize: 160, align: "right", sortKey: "size" },
  { id: "host", title: "Host", size: 150, minSize: 80, maxSize: 320, sortKey: "host", description: "Where the file is hosted" },
  { id: "match", title: "Match", size: 96, minSize: 70, maxSize: 160, sortKey: "match", description: "How likely this is the download you want" },
  { id: "added", title: "Added", size: 78, minSize: 60, maxSize: 140, align: "right", sortKey: "added", description: "When it was found" },
  { id: "url", title: "Address", size: 240, minSize: 120, maxSize: 700, hiddenByDefault: true, description: "Full link" },
  { id: "actions", title: "", size: 56, minSize: 56, maxSize: 56, align: "right", locked: true, description: "Row actions" },
];

export const LOCATION_COLUMNS_KEY = "nexload.columns.vpn-locations.v2";

export const LOCATION_COLUMNS: ColumnDef[] = [
  { id: "status", title: "", size: 30, minSize: 30, maxSize: 30, locked: true, description: "Pick this server" },
  { id: "name", title: "Location", minSize: 150, flex: true, sortKey: "name" },
  { id: "endpoint", title: "Server", size: 180, minSize: 110, maxSize: 400, sortKey: "endpoint", hiddenByDefault: true, description: "WireGuard server address (also shown in the side panel)" },
  { id: "ip", title: "Exit IP", size: 124, minSize: 90, maxSize: 220, description: "Public address seen through this route" },
  { id: "latency", title: "Latency", size: 76, minSize: 60, maxSize: 140, align: "right", sortKey: "latency" },
  { id: "checked", title: "Checked", size: 88, minSize: 64, maxSize: 160, align: "right", hiddenByDefault: true },
  { id: "actions", title: "", size: 36, minSize: 36, maxSize: 36, align: "right", locked: true, description: "Row actions" },
];

export const PROXY_COLUMNS_KEY = "nexload.columns.proxies.v1";

export const PROXY_COLUMNS: ColumnDef[] = [
  { id: "select", title: "", size: 30, minSize: 30, maxSize: 30, locked: true, description: "Select" },
  { id: "status", title: "", size: 22, minSize: 22, maxSize: 22, locked: true, description: "Last check result" },
  { id: "address", title: "Proxy", minSize: 170, flex: true, sortKey: "address" },
  { id: "type", title: "Type", size: 64, minSize: 52, maxSize: 110, sortKey: "type" },
  { id: "latency", title: "Latency", size: 76, minSize: 60, maxSize: 140, align: "right", sortKey: "latency" },
  { id: "ip", title: "Exit IP", size: 130, minSize: 90, maxSize: 220 },
  { id: "checked", title: "Checked", size: 88, minSize: 64, maxSize: 160, align: "right" },
  { id: "enabled", title: "On", size: 46, minSize: 46, maxSize: 46, align: "center", locked: true, description: "Use this proxy" },
  { id: "actions", title: "", size: 34, minSize: 34, maxSize: 34, align: "right", locked: true, description: "Row actions" },
];

export const HISTORY_COLUMNS_KEY = "nexload.columns.history.v3";

export const HISTORY_COLUMNS: ColumnDef[] = [
  { id: "expand", title: "", size: 24, minSize: 24, maxSize: 24, locked: true, description: "Show what happened" },
  { id: "name", title: "File", minSize: 200, flex: true },
  { id: "activity", title: "Activity", size: 104, minSize: 80, maxSize: 200, description: "Captchas and shortlink steps" },
  { id: "size", title: "Size", size: 72, minSize: 52, maxSize: 180, align: "right" },
  { id: "took", title: "Took", size: 64, minSize: 48, maxSize: 160, align: "right" },
  { id: "status", title: "Status", size: 96, minSize: 76, maxSize: 240 },
  { id: "host", title: "Host", size: 130, minSize: 70, maxSize: 260 },
  { id: "folder", title: "Saved to", size: 200, minSize: 110, maxSize: 600, hiddenByDefault: true },
  { id: "finished", title: "Finished", size: 84, minSize: 64, maxSize: 200, align: "right" },
];
