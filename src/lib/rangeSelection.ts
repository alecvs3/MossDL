export function selectRange(order: readonly string[], anchor: string, focus: string): string[] {
  const start = order.indexOf(anchor);
  const end = order.indexOf(focus);
  if (start < 0 || end < 0) return end < 0 ? [] : [focus];
  const from = Math.min(start, end);
  const to = Math.max(start, end);
  return order.slice(from, to + 1);
}

export function toggleSelection(selected: readonly string[], id: string): string[] {
  return selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id];
}

export function pruneSelection(selected: readonly string[], order: readonly string[]): string[] {
  const available = new Set(order);
  return selected.filter((id) => available.has(id));
}
