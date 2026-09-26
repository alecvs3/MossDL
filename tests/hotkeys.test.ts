import assert from "node:assert/strict";
import {
  HOTKEY_COMMANDS,
  assignBinding,
  bindingsFor,
  clearBinding,
  commandForBinding,
  findConflicts,
  formatBinding,
  isTypingTarget,
  normalizeBinding,
  parseHotkeyMap,
  resetBinding,
  resolveBindings,
} from "../src/lib/hotkeys.ts";

// ── normalising ───────────────────────────────────────────────────────────
assert.equal(normalizeBinding("Ctrl+N"), "ctrl+n");
assert.equal(normalizeBinding("shift+CTRL+p"), "ctrl+shift+p", "modifiers land in a fixed order");
assert.equal(normalizeBinding("Cmd+K"), "meta+k");
assert.equal(normalizeBinding("Control + Alt + Delete"), "ctrl+alt+delete");
assert.equal(normalizeBinding("Esc"), "escape");
assert.equal(normalizeBinding("ctrl++"), "ctrl++", "a literal plus survives the separator");
assert.equal(normalizeBinding("ctrl"), "", "a modifier on its own is not a shortcut");
assert.equal(normalizeBinding(""), "");

// ── display ───────────────────────────────────────────────────────────────
assert.equal(formatBinding("ctrl+shift+p"), "Ctrl + Shift + P");
assert.equal(formatBinding("ctrl+`"), "Ctrl + `");
assert.equal(formatBinding("delete"), "Delete");
assert.equal(formatBinding("ctrl+,"), "Ctrl + ,");

// ── the shipped defaults ──────────────────────────────────────────────────
const defaults = resolveBindings({});
assert.deepEqual(defaults["add-url"], ["ctrl+n", "ctrl+o"], "Add download answers to both");
assert.equal(commandForBinding("ctrl+n", {}), "add-url");
assert.equal(commandForBinding("ctrl+o", {}), "add-url");
assert.equal(commandForBinding("ctrl+3", {}), "go-captchas");
assert.equal(commandForBinding("ctrl+j", {}), null, "an unbound key runs nothing");
assert.deepEqual(findConflicts({}), {}, "nothing ships with a clashing shortcut");
// Every command has a usable default and a unique id.
const ids = new Set<string>();
for (const command of HOTKEY_COMMANDS) {
  assert.equal(ids.has(command.id), false, `${command.id} is defined twice`);
  ids.add(command.id);
  for (const binding of command.defaultBindings) {
    assert.equal(normalizeBinding(binding), binding, `${command.id} default is already normalised`);
  }
}

// ── rebinding ─────────────────────────────────────────────────────────────
let map = assignBinding({}, "pause-all", "Ctrl+Shift+Z");
assert.deepEqual(bindingsFor(HOTKEY_COMMANDS.find((c) => c.id === "pause-all")!, map), ["ctrl+shift+z"]);
assert.equal(commandForBinding("ctrl+shift+z", map), "pause-all");
assert.equal(commandForBinding("ctrl+shift+p", map), null, "the old binding is released");

// Taking a shortcut from another command removes it there: no silent clashes.
map = assignBinding(map, "resume-all", "ctrl+shift+z");
assert.equal(commandForBinding("ctrl+shift+z", map), "resume-all");
assert.deepEqual(bindingsFor(HOTKEY_COMMANDS.find((c) => c.id === "pause-all")!, map), []);
assert.deepEqual(findConflicts(map), {}, "reassigning never leaves a duplicate");

// Fixed commands cannot be rebound.
const fixed = assignBinding({}, "select-all", "ctrl+q");
assert.deepEqual(fixed, {}, "fixed shortcuts are left alone");

// Clearing and resetting.
const cleared = clearBinding({}, "zoom-reset");
assert.deepEqual(cleared["zoom-reset"], []);
assert.equal(commandForBinding("ctrl+0", cleared), null);
assert.equal(commandForBinding("ctrl+0", resetBinding(cleared, "zoom-reset")), "zoom-reset");

// ── stored maps ───────────────────────────────────────────────────────────
assert.deepEqual(parseHotkeyMap(JSON.stringify({ "pause-all": ["Ctrl+Shift+Z"] })), { "pause-all": ["ctrl+shift+z"] });
assert.deepEqual(parseHotkeyMap(JSON.stringify({ "pause-all": "ctrl+shift+z" })), { "pause-all": ["ctrl+shift+z"] },
  "a single string works as well as a list");
assert.deepEqual(parseHotkeyMap(JSON.stringify({ "no-such-command": ["ctrl+k"] })), {}, "unknown commands are dropped");
assert.deepEqual(parseHotkeyMap("not json"), {});
assert.deepEqual(parseHotkeyMap(""), {});
assert.deepEqual(parseHotkeyMap(undefined), {});

// ── typing guard ──────────────────────────────────────────────────────────
assert.equal(isTypingTarget({ tagName: "INPUT" } as unknown as EventTarget), true);
assert.equal(isTypingTarget({ tagName: "TEXTAREA" } as unknown as EventTarget), true);
assert.equal(isTypingTarget({ tagName: "DIV", isContentEditable: true } as unknown as EventTarget), true);
assert.equal(isTypingTarget({ tagName: "DIV" } as unknown as EventTarget), false);
assert.equal(isTypingTarget(null), false);

console.log("Hotkey tests passed");
