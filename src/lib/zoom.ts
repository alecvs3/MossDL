export const MIN_ZOOM = 80;
export const MAX_ZOOM = 160;
export const DEFAULT_ZOOM = 100;
/** Readability floor: the comfortable scale on a standard window. */
export const BASE_SCALE = 112;

export function clampZoom(value: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(value)));
}

export function nextZoom(current: number, delta: number): number {
  return clampZoom(current + delta);
}

/**
 * Calculates optimal scale percentage based on window viewport width,
 * monitor physical width, and fullscreen/maximized status.
 */
export function calculateAutoWideScale(
  viewportWidth: number,
  isMaximizedOrFullscreen: boolean,
  fullscreenBoost = true
): number {
  // The base sizes in the components are small (most text is 9-11px), so the
  // readable floor is above 100%. Everything scales together, keeping layout intact.
  let scale = BASE_SCALE;

  if (viewportWidth >= 3400) {
    // 4K or 34"+ ultrawide
    scale = 140;
  } else if (viewportWidth >= 2560) {
    // 1440p standard or 29" ultrawide
    scale = 130;
  } else if (viewportWidth >= 1920) {
    // 1080p full width
    scale = 122;
  } else if (viewportWidth >= 1600) {
    // Sub-1080p or narrow 1440p
    scale = 118;
  } else {
    scale = BASE_SCALE;
  }

  if (fullscreenBoost && isMaximizedOrFullscreen) {
    scale += 6;
  }

  return clampZoom(scale);
}

