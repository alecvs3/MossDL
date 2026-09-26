// One place that knows every keyboard shortcut in the app: what commands exist,
// what they are bound to, and how a key press maps back to a command.
//
// Bindings are stored as normalised strings ("ctrl+shift+p"). A command can
// have several bindings, which is how Add download answers to both Ctrl+N and
// Ctrl+O.

export type HotkeyGroup = "General" | "Transfers" | "Navigation" | "View";

export interface HotkeyCommand {
  id: string;
  label: string;
  group: HotkeyGroup;
  defaultBindings: string[];
  description?: string;
  /** Fixed shortcuts the user cannot rebind (system or text editing). */
  fixed?: boolean;
}

export const HOTKEY_COMMANDS: HotkeyCommand[] = [
  { id: "add-url", label: "Add download", group: "General", defaultBindings: ["ctrl+n", "ctrl+o"],
    description: "Open the Add window, pasting a link from the clipboard" },
  { id: "add-from-clipboard", label: "Add link from clipboard", group: "General", defaultBindings: ["ctrl+shift+v"],
    description: "Add whatever link is on the clipboard without opening the window" },
  { id: "focus-search", label: "Search this list", group: "General", defaultBindings: ["ctrl+f"] },
  { id: "settings", label: "Settings", group: "General", defaultBindings: ["ctrl+,"] },

  { id: "pause-all", label: "Pause all", group: "Transfers", defaultBindings: ["ctrl+shift+p"] },
  { id: "resume-all", label: "Resume all", group: "Transfers", defaultBindings: ["ctrl+shift+r"] },
  { id: "clear-finished", label: "Clear finished", group: "Transfers", defaultBindings: ["ctrl+shift+l"] },
  { id: "delete-selected", label: "Remove selected", group: "Transfers", defaultBindings: ["delete"] },
  { id: "select-all", label: "Select everything", group: "Transfers", defaultBindings: ["ctrl+a"], fixed: true },

  { id: "go-downloads", label: "Downloads", group: "Navigation", defaultBindings: ["ctrl+1"] },
  { id: "go-grabber", label: "Explore", group: "Navigation", defaultBindings: ["ctrl+2"] },
  { id: "go-captchas", label: "Captchas", group: "Navigation", defaultBindings: ["ctrl+3"] },
  { id: "go-routes", label: "Connections", group: "Navigation", defaultBindings: ["ctrl+4"] },
  { id: "go-history", label: "History", group: "Navigation", defaultBindings: ["ctrl+5"] },

  { id: "zoom-in", label: "Zoom in", group: "View", defaultBindings: ["ctrl+=", "ctrl++"] },
  { id: "zoom-out", label: "Zoom out", group: "View", defaultBindings: ["ctrl+-"] },
  { id: "zoom-reset", label: "Reset zoom", group: "View", defaultBindings: ["ctrl+0"] },
  { id: "dev-logs", label: "Diagnostics console", group: "View", defaultBindings: ["ctrl+`"] },
];

export const HOTKEY_GROUPS: HotkeyGroup[] = ["General", "Transfers", "Navigation", "View"];

/** command id → bindings. Only commands the user changed are stored. */
export type HotkeyMap = Record<string, string[]>;

const MODIFIER_ORDER = ["ctrl", "alt", "shift", "meta"] as const;

const KEY_ALIASES: Record<string, string> = {
  esc: "escape",
  del: "delete",
  ins: "insert",
  spacebar: " ",
  space: " ",
  plus: "+",
  equal: "=",
  arrowup: "up",
  arrowdown: "down",
  arrowleft: "left",
  arrowright: "right",
};

const DISPLAY_KEYS: Record<string, string> = {
  " ": "Space",
  escape: "Esc",
  delete: "Delete",
  arrowup: "↑",
  up: "↑",
  down: "↓",
  left: "←",
  right: "→",
  "`": "`",
  ",": ",",
  "=": "=",
  "-": "−",
  "+": "+",
};

/** Turns any spelling of a binding into the canonical "ctrl+shift+p" form. */
export function normalizeBinding(binding: string): string {
  const parts = String(binding || "")
    .toLowerCase()
    .split("+")
    .map((part) => part.trim())
    .filter(Boolean);
  // A trailing "+" (as in "ctrl++") leaves an empty tail; put the key back.
  if (/\+\s*\+\s*$/.test(binding)) parts.push("+");
  const modifiers = new Set<string>();
  let key = "";
  for (const part of parts) {
    if (part === "control" || part === "ctrl") modifiers.add("ctrl");
    else if (part === "alt" || part === "option") modifiers.add("alt");
    else if (part === "shift") modifiers.add("shift");
    else if (part === "meta" || part === "cmd" || part === "win" || part === "super") modifiers.add("meta");
    else key = KEY_ALIASES[part] ?? part;
  }
  if (!key) return "";
  return [...MODIFIER_ORDER.filter((mod) => modifiers.has(mod)), key].join("+");
}

/** The binding a key press represents, or "" when it is only a modifier. */
export function bindingFromEvent(event: KeyboardEvent): string {
  const key = event.key;
  if (!key || ["Control", "Alt", "Shift", "Meta", "Dead"].includes(key)) return "";
  const parts: string[] = [];
  if (event.ctrlKey) parts.push("ctrl");
  if (event.altKey) parts.push("alt");
  if (event.shiftKey) parts.push("shift");
  if (event.metaKey) parts.push("meta");
  parts.push(key.length === 1 ? key.toLowerCase() : key.toLowerCase());
  return normalizeBinding(parts.join("+"));
}

/** Human form for the UI: "ctrl+shift+p" → "Ctrl + Shift + P". */
export function formatBinding(binding: string): string {
  const normalized = normalizeBinding(binding);
  if (!normalized) return "";
  return normalized
    .split("+")
    .filter((part, index, all) => part !== "" || index === all.length - 1)
    .map((part) => {
      if (part === "ctrl") return "Ctrl";
      if (part === "alt") return "Alt";
      if (part === "shift") return "Shift";
      if (part === "meta") return "Win";
      return DISPLAY_KEYS[part] ?? (part.length === 1 ? part.toUpperCase() : part.replace(/^./, (c) => c.toUpperCase()));
    })
    .join(" + ");
}

export function bindingsFor(command: HotkeyCommand, map: HotkeyMap): string[] {
  const custom = map[command.id];
  const source = custom ?? command.defaultBindings;
  return source.map(normalizeBinding).filter(Boolean);
}

/** command id → bindings, for every command. */
export function resolveBindings(map: HotkeyMap, commands = HOTKEY_COMMANDS): Record<string, string[]> {
  const resolved: Record<string, string[]> = {};
  for (const command of commands) resolved[command.id] = bindingsFor(command, map);
  return resolved;
}

/** Which command a press should run, or null. Later commands never win twice. */
export function commandForBinding(binding: string, map: HotkeyMap, commands = HOTKEY_COMMANDS): string | null {
  const normalized = normalizeBinding(binding);
  if (!normalized) return null;
  for (const command of commands) {
    if (bindingsFor(command, map).includes(normalized)) return command.id;
  }
  return null;
}

/** binding → the commands that share it, for anything used more than once. */
export function findConflicts(map: HotkeyMap, commands = HOTKEY_COMMANDS): Record<string, string[]> {
  const owners: Record<string, string[]> = {};
  for (const command of commands) {
    for (const binding of bindingsFor(command, map)) {
      (owners[binding] ??= []).push(command.id);
    }
  }
  return Object.fromEntries(Object.entries(owners).filter(([, ids]) => ids.length > 1));
}

/** Assigns a binding to a command, taking it from whoever else held it. */
export function assignBinding(map: HotkeyMap, commandId: string, binding: string, commands = HOTKEY_COMMANDS): HotkeyMap {
  const normalized = normalizeBinding(binding);
  const command = commands.find((item) => item.id === commandId);
  if (!normalized || !command || command.fixed) return map;
  const next: HotkeyMap = { ...map };
  for (const other of commands) {
    const current = bindingsFor(other, next);
    if (other.id === commandId) {
      next[commandId] = [normalized];
    } else if (current.includes(normalized) && !other.fixed) {
      next[other.id] = current.filter((item) => item !== normalized);
    }
  }
  return next;
}

export function clearBinding(map: HotkeyMap, commandId: string): HotkeyMap {
  return { ...map, [commandId]: [] };
}

export function resetBinding(map: HotkeyMap, commandId: string): HotkeyMap {
  const next = { ...map };
  delete next[commandId];
  return next;
}

export function parseHotkeyMap(raw: unknown): HotkeyMap {
  if (typeof raw !== "string" || !raw.trim()) return {};
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object") return {};
    const map: HotkeyMap = {};
    for (const [id, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!HOTKEY_COMMANDS.some((command) => command.id === id)) continue;
      if (Array.isArray(value)) map[id] = value.map(String).map(normalizeBinding).filter(Boolean);
      else if (typeof value === "string") map[id] = [normalizeBinding(value)].filter(Boolean);
    }
    return map;
  } catch {
    return {};
  }
}

/** True when a key press should be left to the focused control. */
export function isTypingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element || !element.tagName) return false;
  const tag = element.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || Boolean(element.isContentEditable);
}
