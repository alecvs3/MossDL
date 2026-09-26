import assert from "node:assert/strict";
import { selectRange, toggleSelection, pruneSelection } from "../src/lib/rangeSelection.ts";
import { clampZoom, nextZoom, MIN_ZOOM, MAX_ZOOM } from "../src/lib/zoom.ts";
import { getCommandState } from "../src/figma/commands.ts";
import { settingDefinition, validateSetting } from "../src/figma/settingsSchema.ts";
import { overlayPriority, shouldRenderNowPlaying } from "../src/lib/layout.ts";

assert.deepEqual(selectRange(["a", "b", "c", "d"], "b", "d"), ["b", "c", "d"]);
assert.deepEqual(selectRange(["a", "b", "c", "d"], "d", "b"), ["b", "c", "d"]);
assert.deepEqual(toggleSelection(["a", "b"], "a"), ["b"]);
assert.deepEqual(toggleSelection(["b"], "a"), ["b", "a"]);
assert.deepEqual(pruneSelection(["a", "missing"], ["a", "b"]), ["a"]);

assert.equal(clampZoom(MIN_ZOOM - 10), MIN_ZOOM);
assert.equal(clampZoom(MAX_ZOOM + 20), MAX_ZOOM);
assert.equal(nextZoom(100, 10), 110);
assert.equal(nextZoom(80, -10), 80);
assert.equal(shouldRenderNowPlaying("downloading", false), true);
assert.equal(shouldRenderNowPlaying("completed", false), false);
assert.equal(shouldRenderNowPlaying("cancelled", true), true);
assert.ok(overlayPriority.playMenu > overlayPriority.nowPlaying);
assert.ok(overlayPriority.modal > overlayPriority.contextMenu);

assert.equal(getCommandState("delete-selected", { selectedCount: 0, capabilities: {} }).enabled, false);
assert.equal(getCommandState("delete-selected", { selectedCount: 1, capabilities: {} }).enabled, true);
assert.equal(getCommandState("browser-capture", { selectedCount: 0, capabilities: {} }).enabled, false);

const maxConcurrent = settingDefinition("general", "maxConcurrent");
assert.equal(maxConcurrent?.type, "number");
assert.equal(validateSetting(maxConcurrent!, "0").ok, false);
assert.equal(validateSetting(maxConcurrent!, "4").ok, true);

console.log("ui interaction tests passed");
