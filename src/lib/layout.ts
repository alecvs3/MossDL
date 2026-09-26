export const overlayPriority = {
  applicationChrome: 40,
  nowPlaying: 45,
  playMenu: 60,
  contextMenu: 90,
  modal: 100,
} as const;

const activeTransferStates = new Set(["downloading", "paused", "queued"]);

export function shouldRenderNowPlaying(status: string | undefined, recentlyStopped: boolean): boolean {
  return recentlyStopped || activeTransferStates.has(status || "");
}
