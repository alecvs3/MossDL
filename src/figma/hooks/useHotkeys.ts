// Runs the app's keyboard shortcuts. One listener owns every binding, so
// shortcuts stay consistent and a rebind takes effect immediately.
import { useEffect, useRef } from "react";
import {
  bindingFromEvent,
  commandForBinding,
  isTypingTarget,
  type HotkeyMap,
} from "../../lib/hotkeys";

export type HotkeyHandlers = Record<string, (() => void) | undefined>;

/** Shortcuts that still work while typing, because they are not text input. */
const ALLOWED_WHILE_TYPING = new Set(["settings", "dev-logs", "zoom-in", "zoom-out", "zoom-reset"]);

export function useHotkeys(map: HotkeyMap, handlers: HotkeyHandlers, enabled = true): void {
  // Handlers change on every render; a ref keeps one stable listener.
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  const mapRef = useRef(map);
  mapRef.current = map;

  useEffect(() => {
    if (!enabled) return;
    const onKeyDown = (event: KeyboardEvent) => {
      // A page that handled the key itself (Explore's Delete, a table's Ctrl+A) wins.
      if (event.defaultPrevented) return;
      const binding = bindingFromEvent(event);
      if (!binding) return;
      const commandId = commandForBinding(binding, mapRef.current);
      if (!commandId) return;
      const handler = handlersRef.current[commandId];
      if (!handler) return;
      if (isTypingTarget(event.target) && !ALLOWED_WHILE_TYPING.has(commandId)) return;
      event.preventDefault();
      handler();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}
