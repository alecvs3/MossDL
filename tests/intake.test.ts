import assert from "node:assert/strict";
import { extractClipboardUrls } from "../src/intake.ts";

const urls = extractClipboardUrls([
  "https://example.com/a",
  "https://example.com/a.",
  "javascript:alert(1)",
  "https://user:password@example.com/private",
  "https://example.com/b?x=1",
].join("\n"));

assert.deepEqual(urls, ["https://example.com/a", "https://example.com/b?x=1"]);
console.log("clipboard intake tests passed");
