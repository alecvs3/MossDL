import assert from "node:assert/strict";
import { groupPageLinks, linkFileName, splitPastedLinks, type PageLink } from "../src/lib/pageLinks.ts";

// Shaped like the examplepack ENDLESS Legend 2 page: one 4-part archive on three
// mirrors, an optional bonus file on the same mirrors, and navigation links.
const part = (host: string, provider: string | null, n: number, sep = "--"): PageLink => ({
  id: `${host}-${n}`,
  text: `ENDLESS_Legend_2 part ${n}`,
  target_url: `https://${host}/id${n}/ENDLESS_Legend_2_${sep}_example-repacks.test_${sep}_.part${n}.rar`,
  host,
  category: "high_utility",
  provider_id: provider,
  filename_hint: `ENDLESS_Legend_2_${sep}_example-repacks.test_${sep}_.part${n}.rar`,
  is_multipart: true,
  multipart_part: n,
  package_name: "ENDLESS Legend 2",
});
const bonus = (host: string, provider: string | null): PageLink => ({
  id: `${host}-bonus`,
  text: "fg-optional-bonus-content.bin",
  target_url: `https://${host}/b/fg-optional-bonus-content.bin`,
  host,
  category: "candidate",
  provider_id: provider,
  filename_hint: "fg-optional-bonus-content.bin",
});

const links: PageLink[] = [
  // FileKeeper listed first but has no dedicated provider: DataNodes should win.
  ...[4, 3, 2, 1].map((n) => part("filekeeper.net", "generic", n)),
  ...[1, 2, 3, 4].map((n) => part("datanodes.to", "datanodes", n)),
  ...[1, 2, 3, 4].map((n) => part("fuckingfast.co", "fuckingfast", n, "-")),
  part("datanodes.to", "datanodes", 1), // duplicate anchor for the same part
  bonus("datanodes.to", "datanodes"),
  bonus("fuckingfast.co", "fuckingfast"),
  { id: "nav", text: "Red Dead Redemption 2", target_url: "https://example-repacks.test/rdr2/", host: "example-repacks.test", category: "secondary" },
  { id: "mag", text: "magnet", target_url: "magnet:?xt=urn:btih:abc", host: "", category: "secondary", unsupported: true },
];

const { groups, other } = groupPageLinks(links);
assert.equal(groups.length, 2, "one package and one optional file");

const pkg = groups[0];
assert.equal(pkg.isPackage, true);
assert.equal(pkg.optional, false);
assert.equal(pkg.mirrors.length, 3, "mirrors are merged into one package");
assert.equal(pkg.defaultMirror, "datanodes.to", "a supported host beats page order");
const datanodes = pkg.mirrors.find((mirror) => mirror.host === "datanodes.to")!;
assert.equal(datanodes.links.length, 4, "duplicate anchors collapse");
const filekeeper = pkg.mirrors.find((mirror) => mirror.host === "filekeeper.net")!;
assert.deepEqual(filekeeper.links.map((link) => link.multipart_part), [1, 2, 3, 4], "parts are ordered");

const extra = groups[1];
assert.equal(extra.optional, true, "candidate-only groups are optional and listed last");
assert.equal(extra.mirrors.length, 2);
assert.equal(other.map((link) => link.id).join(","), "nav,mag");

assert.equal(linkFileName(links[0]), "ENDLESS_Legend_2_--_example-repacks.test_--_.part4.rar");
assert.equal(linkFileName({ ...links[0], filename_hint: "", target_url: "https://host/file/abc" }), undefined,
  "link text is never used as a filename");

assert.deepEqual(
  splitPastedLinks("see https://a.example/x.zip, and\nhttps://b.example/y (mirror) https://a.example/x.zip"),
  ["https://a.example/x.zip", "https://b.example/y"],
);
assert.deepEqual(splitPastedLinks("datanodes.to/abc/file.rar"), ["datanodes.to/abc/file.rar"]);
assert.deepEqual(
  splitPastedLinks("https://a.example/x.ziphttps://b.example/f.exe"),
  ["https://a.example/x.zip", "https://b.example/f.exe"],
  "links pasted back to back are split",
);
for (const embedded of ["https://go.example/?url=https://t.example/f.zip", "https://s.example/go/https://t.example/f.zip"]) {
  assert.deepEqual(splitPastedLinks(embedded), [embedded], "shortlinks that embed a target stay whole");
}
assert.deepEqual(splitPastedLinks("magnet:?xt=urn:btih:abc"), ["magnet:?xt=urn:btih:abc"]);
assert.deepEqual(splitPastedLinks("   "), []);

console.log("Page link grouping tests passed");
