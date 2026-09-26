/**
 * Canonical formatting utilities for bytes, transfer rates, and durations.
 * Single source of truth across the MossDL frontend.
 */

export function formatBytes(bytes?: number | null): string {
  if (bytes === undefined || bytes === null || isNaN(bytes)) return 'Unknown size';
  if (bytes <= 0) return '0 B';
  if (bytes < 1024) return String(bytes) + ' B';

  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let val = bytes;
  let unit = 'B';

  for (const u of units) {
    val /= 1024;
    unit = u;
    if (val < 1024) break;
  }

  const dec = val >= 100 ? 0 : 1;
  return val.toFixed(dec) + ' ' + unit;
}

export function formatSpeed(bytesPerSec?: number | null): string {
  if (!bytesPerSec || bytesPerSec <= 0 || isNaN(bytesPerSec)) return '0 B/s';
  return formatBytes(bytesPerSec) + '/s';
}

export function formatPercent(current: number, total: number): number {
  if (!total || total <= 0 || isNaN(total)) return 0;
  return Math.min(100, Math.max(0, Math.round((Math.min(current, total) / total) * 100)));
}

export function formatDuration(seconds?: number | null): string {
  if (!seconds || seconds <= 0 || !isFinite(seconds)) return '--';
  const s = Math.round(seconds);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  const remS = s % 60;
  if (m < 60) return m + 'm ' + remS + 's';
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return h + 'h ' + remM + 'm';
}
