import assert from "node:assert/strict";
import {
  allFileIds,
  buildPickerTree,
  descendantFiles,
  initiallyExpanded,
} from "../src/treePicker.ts";
import type { ProviderTreeItem } from "../src/treePicker.ts";

function folder(id: string, parent_id?: string, childCount = 0): ProviderTreeItem {
  return { item_id: id, display_name: id, relative_path: id, metadata: { type: "folder", node: id, parent_id, childCount } };
}

function file(id: string, parent_id: string): ProviderTreeItem {
  return { item_id: id, display_name: id, relative_path: `${parent_id}/${id}`, metadata: { type: "file", node: id, parent_id } };
}

function childrenCount(items: ProviderTreeItem[], parent: string, count: number, prefix: string): ProviderTreeItem[] {
  return Array.from({ length: count }, (_, index) => file(`${prefix}-${index}`, parent));
}

function flatten(nodes: ReturnType<typeof buildPickerTree>): ReturnType<typeof buildPickerTree> {
  return nodes.flatMap((node) => [node, ...flatten(node.children)]);
}

{
  const items = [folder("root"), folder("large", "root"), ...childrenCount([], "large", 10, "file")];
  const tree = buildPickerTree(items);
  const expanded = initiallyExpanded(tree);
  assert(expanded.includes("root"));
  assert(!expanded.includes("large"));
}

for (const count of [5, 6]) {
  const items = [folder("root"), folder("branch", "root"), ...childrenCount([], "branch", count, `file-${count}`)];
  const expanded = initiallyExpanded(buildPickerTree(items));
  assert.equal(expanded.includes("branch"), count === 5);
}

{
  const items: ProviderTreeItem[] = [folder("root")];
  let parent = "root";
  for (let level = 0; level < 12; level += 1) {
    const next = `level-${level}`;
    items.push(folder(next, parent));
    parent = next;
  }
  const expanded = initiallyExpanded(buildPickerTree(items));
  assert(expanded.includes("level-6"));
  assert(!expanded.includes("level-7"));
}

{
  const items: ProviderTreeItem[] = [folder("root")];
  let parents = ["root"];
  for (let level = 0; level < 4; level += 1) {
    const next: string[] = [];
    for (const parent of parents) {
      for (let index = 0; index < 5; index += 1) {
        const child = `${parent}-${level}-${index}`;
        items.push(folder(child, parent));
        next.push(child);
      }
    }
    parents = next;
  }
  const tree = buildPickerTree(items);
  const expanded = initiallyExpanded(tree);
  assert(expanded.length < items.length);
  assert(flatten(tree).some((node) => node.autoCollapsedReason === "preview limit"));
}

{
  const items = [folder("root"), folder("a", "root"), file("one", "a"), file("two", "a")];
  const tree = buildPickerTree(items);
  assert.deepEqual(descendantFiles(tree[0]), ["one", "two"]);
  assert.deepEqual(allFileIds(tree), ["one", "two"]);
}

console.log("tree picker tests passed");
