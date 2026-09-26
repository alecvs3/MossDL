export function extractClipboardUrls(value: string): string[] {
  const found = value.match(/https?:\/\/[^\s<>"']+/gi) || [];
  const unique = new Set<string>();
  for (const raw of found) {
    const candidate = raw.replace(/[),.;!?]+$/, "");
    try {
      const parsed = new URL(candidate);
      if (!parsed.hostname || parsed.username || parsed.password) continue;
      unique.add(parsed.toString());
    } catch { /* ignore malformed clipboard fragments */ }
  }
  return [...unique];
}
