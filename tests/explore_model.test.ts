import assert from "node:assert/strict";
import { buildTree, siteOf, walk, type ExploreData } from "../src/figma/pages/explore/exploreModel.ts";
import {
  checkState, facetCounts, initialView, isOpen, selectedFiles, setChecked, visibleRows, type ViewState,
} from "../src/figma/pages/explore/exploreView.ts";

const el = (id: string, o: Record<string, unknown>) => ({
  id, tag: "a", text: id, target_url: `https://host.test/${id}`, host: "host.test", score: 50,
  category: "candidate", is_shortlink: false, ...o,
});

const data: ExploreData = {
  crawls: [{
    id: "c1", url: "https://blog.test/post", loading: false, headless: false, startedAt: 1,
    response: {
      url: "https://blog.test/post", title: "A post", total_elements: 5, ads_stripped: 2, used_headless: false, has_countdown_timer: false,
      elements: [
        el("movie", { target_url: "https://gofile.io/d/x/movie.mkv", filename_hint: "movie.mkv", file_kind: "video", score: 95, category: "high_utility" }),
        el("song", { target_url: "https://cdn.test/song.flac", file_kind: "music", score: 80, category: "high_utility" }),
        el("btn", { tag: "button", text: "Get Link", target_url: "https://short.test/abc", is_shortlink: true, score: 70 }),
        el("about", { target_url: "https://blog.test/about", category: "secondary", score: 5 }),
        el("p1", { target_url: "https://m.test/pack.part1.rar", file_kind: "archives", is_multipart: true, package_name: "pack", category: "high_utility" }),
        el("p2", { target_url: "https://m.test/pack.part2.rar", file_kind: "archives", is_multipart: true, package_name: "pack", category: "high_utility" }),
      ],
    } as never,
  }, {
    id: "c2", url: "https://short.test/abc", parentId: "m:c1:btn", loading: true, headless: true, startedAt: 2,
  }],
  resolutions: {}, resolveErrors: {}, resolving: new Set(),
  batches: [{
    batch_id: "b1", request_id: "r", protocol_version: "1", extension_origin: "x", page_origin: "https://yt.test", page_url: "https://yt.test/w",
    state: "pending", acknowledged: false, imported_task_ids: [], created_at: 0, updated_at: 0,
    candidates: [{ url: "https://cdn.yt.test/v.mp4", filename: "v.mp4", file_kind: "video", confidence: 0.9, size: 10 }],
  }],
  entries: [],
};

const tree = buildTree(data);
assert.equal(tree.length, 2, "a crawl and a capture at the top; the followed crawl is nested");
assert.equal(tree[0].via, "Explored");
assert.equal(tree[1].via, "Captured");

const ids: string[] = [];
walk(tree, (n) => ids.push(n.id));
assert.ok(ids.includes("crawl:c2"), "followed crawl hangs under the row it came from");
const btn = tree[0].children.flatMap((g) => g.children).find((n) => n.id === "m:c1:btn");
assert.equal(btn?.children[0]?.id, "crawl:c2");
assert.equal(btn?.children[0]?.busy, "Opening in browser…");

const detected = tree[0].children.find((g) => g.label === "Detected downloads")!;
assert.equal(detected.children[0].type, "folder", "multi-part files are packaged");
assert.equal(detected.children[0].children.length, 2);
const song = detected.children.find((n) => n.label === "song.flac");
assert.equal(song?.kind, "audio", "engine 'music' shows as Audio");
assert.ok(detected.children.find((n) => n.label === "movie.mkv")?.best, "top score is starred");

// Filtering by kind keeps ancestors of matching files and drops the rest.
const videoOnly: ViewState = { ...initialView, kind: "video" };
const rows = visibleRows(tree, videoOnly).map((r) => r.node.label);
assert.ok(rows.includes("movie.mkv") && rows.includes("v.mp4") && !rows.includes("song.flac"));

// Checking a group checks every file under it; parents become tri-state.
let sel = setChecked(new Set(), [detected], true, initialView);
assert.equal(checkState(detected, sel, initialView), "on");
sel = setChecked(sel, [song!], false, initialView);
assert.equal(checkState(detected, sel, initialView), "mixed");
assert.deepEqual(selectedFiles(tree, sel, initialView).map((n) => n.label).sort(), ["movie.mkv", "pack.part1.rar", "pack.part2.rar"]);

// Facets count downloadable files only.
const facets = facetCounts(tree, initialView);
assert.equal(facets.kinds.all, 5);
assert.equal(facets.kinds.video, 2);

// Grouping by kind re-slices the same rows.
const byKind = visibleRows(tree, { ...initialView, groupBy: "kind" }).filter((r) => r.node.type === "group").map((r) => r.node.label);
assert.deepEqual(byKind.sort(), ["Archives", "Audio", "Video"]);

// A resolved shortlink ending at a host root is a link, not a file named after the host.
const resolved = buildTree({ ...data, crawls: [data.crawls[0]], batches: [], resolutions: {
  "m:c1:btn": { final_url: "https://1fichier.com/?abc", hops: [{ hop_number: 1, input_url: "https://short.test/abc", output_url: "https://1fichier.com/?abc", strategy: "redirect", status: "ok" }] },
} });
const finalNode = resolved[0].children.flatMap((g) => g.children).find((n) => n.id === "m:c1:btn")!.children.at(-1)!;
assert.equal(finalNode.type, "link");
assert.equal(finalNode.label, "1fichier.com/?abc");

// A followed button names its file from the server, and a stop says why.
const followed = (resolution: object) => buildTree({ ...data, crawls: [data.crawls[0]], batches: [], resolutions: { "m:c1:btn": resolution as never } })[0]
  .children.flatMap((g) => g.children).find((n) => n.id === "m:c1:btn")!;
const reached = followed({ final_url: "https://cdn.test/dl?id=9", direct_url: "https://cdn.test/dl?id=9", filename: "Game v1.2.zip",
  hops: [{ hop_number: 1, input_url: "https://a.test/", output_url: "https://cdn.test/dl?id=9", strategy: "clicked “Free Download”", status: "ok" }] });
assert.equal(reached.children.at(-1)!.label, "Game v1.2.zip");
assert.equal(reached.children.at(-1)!.type, "file");
const stopped = followed({ final_url: "https://a.test/wait", hops: [], captcha: true, error: null });
assert.equal(stopped.tag?.tone, "warn");

// Captures of the same page merge into one row; noise is tucked away and low.
const batch = (id: string, cands: object[]) => ({ batch_id: id, request_id: id, protocol_version: "1", extension_origin: "x", page_origin: "https://rt.test",
  page_url: "https://rt.test/t=1", state: "pending", acknowledged: false, imported_task_ids: [], created_at: 1, updated_at: 1, candidates: cands as never });
const merged = buildTree({ ...data, crawls: [], batches: [
  batch("b1", [{ url: "https://cdn.test/1.mp4", filename: "1.mp4", file_kind: "video", confidence: 1 }]),
  batch("b2", [{ url: "https://cdn.test/1.mp4", filename: "1.mp4", file_kind: "video", confidence: 1 },
    { url: "https://x.challenges.cloudflare.com/c", file_kind: "other", confidence: 1, noise: "site infrastructure" }]),
] });
assert.equal(merged.length, 1);
assert.deepEqual(merged[0].batchIds, ["b1", "b2"]);
assert.equal(merged[0].children.filter((c) => c.type === "file").length, 1, "same address once");
const noiseGroup = merged[0].children.find((c) => c.type === "group")!;
assert.ok(noiseGroup.low && noiseGroup.children[0].low);
assert.equal(facetCounts(merged, initialView).kinds.all, 1, "noise is not counted in filters");

// Only promising branches start open.
assert.equal(isOpen(merged[0], initialView), true, "page with a 100% file opens");
assert.equal(isOpen(noiseGroup, initialView), false);
const possible = tree[0].children.find((g) => g.label === "Possible buttons")!;
assert.equal(isOpen(possible, initialView), false, "no high scorer inside, so collapsed");

// Several hosts can be shown at once.
const twoHosts = visibleRows(tree, { ...initialView, hosts: ["gofile.io", "cdn.yt.test"] }).filter((r) => r.node.type === "file").map((r) => r.node.label);
assert.deepEqual(twoHosts.sort(), ["movie.mkv", "v.mp4"]);

// Grouping by site: two pages on one site sit under one site header.
{
  assert.equal(siteOf("https://v3.rutrk.org/a"), "rutrk.org");
  assert.equal(siteOf("https://files.example.co.uk/x"), "example.co.uk");
  const twoPages = buildTree({ ...data, crawls: [], batches: [
    batch("p1", [{ url: "https://a.test/1.mp4", filename: "1.mp4", file_kind: "video", confidence: 1 }]),
    { ...batch("p2", [{ url: "https://b.test/2.mp4", filename: "2.mp4", file_kind: "video", confidence: 0.5 }]), page_url: "https://rt.test/other" },
  ] });
  const siteRows = visibleRows(twoPages, initialView);
  assert.equal(siteRows[0].node.type, "site");
  assert.equal(siteRows[0].node.children.length, 2);
  assert.equal(siteRows[1].depth, 1, "pages sit under the site");
}

// An index page with only post links shows them open, once each, as "Pages on this site".
{
  const idx = buildTree({ ...data, batches: [], crawls: [{ id: "i1", url: "https://blog.test/", loading: false, headless: false, startedAt: 1,
    response: { url: "https://blog.test/", title: "Blog", total_elements: 4, ads_stripped: 0, used_headless: false, has_countdown_timer: false, elements: [
      el("p1", { target_url: "https://www.blog.test/game-one-v1-2/", text: "Game One (V1.2)", category: "secondary", score: 15 }),
      el("p1b", { target_url: "https://www.blog.test/game-one-v1-2/", text: "Game One (V1.2) full title", category: "secondary", score: 15 }),
      el("cat", { target_url: "https://www.blog.test/category/games/", text: "Category", category: "secondary", score: 0 }),
      el("ext", { target_url: "https://elsewhere.test/x", text: "Some other long link", category: "secondary", score: 0 }),
    ] } as never }] });
  const posts = idx[0].children.find((g) => g.label.startsWith("Pages on this site"))!;
  assert.equal(posts.children.length, 1, "thumbnail and title link collapse to one");
  assert.equal(posts.children[0].label, "Game One (V1.2) full title");
  assert.equal(isOpen(posts, initialView), true);
}

console.log("Explore model tests passed");
