export type ProviderTreeItem = {
  item_id?: string;
  display_name: string;
  relative_path: string;
  size?: number | null;
  metadata: Record<string, unknown>;
};

export type PickerNode = ProviderTreeItem & {
  id: string;
  folder: boolean;
  children: PickerNode[];
  fileCount: number;
  directChildCount: number;
  autoCollapsedReason?: "large branch" | "depth limit" | "preview limit";
};

export const AUTO_EXPANSION_CHILD_LIMIT = 5;
export const AUTO_EXPANSION_NODE_LIMIT = 120;
export const AUTO_EXPANSION_DEPTH_LIMIT = 8;

function nodeKey(node: PickerNode): string {
  return String(node.metadata.node || node.id);
}

export function buildPickerTree(items: ProviderTreeItem[]): PickerNode[] {
  const nodes = items.map((item, index) => ({
    ...item,
    id: item.item_id || `item-${index}`,
    folder: item.metadata?.type === "folder",
    children: [] as PickerNode[],
    fileCount: 0,
    directChildCount: 0,
  }));
  const byNode = new Map<string, PickerNode>();
  for (const node of nodes) {
    byNode.set(node.id, node);
    byNode.set(nodeKey(node), node);
  }
  const roots: PickerNode[] = [];
  for (const node of nodes) {
    const parentId = node.metadata?.parent_id;
    const parent = parentId === undefined || parentId === null || parentId === ""
      ? undefined : byNode.get(String(parentId));
    if (parent?.folder) parent.children.push(node);
    else roots.push(node);
  }
  const annotate = (node: PickerNode): number => {
    node.children.sort((left, right) => Number(right.folder) - Number(left.folder) ||
      left.display_name.localeCompare(right.display_name));
    node.directChildCount = node.children.length;
    node.fileCount = node.folder ? node.children.reduce((sum, child) => sum + annotate(child), 0) : 1;
    return node.fileCount;
  };
  roots.sort((left, right) => Number(right.folder) - Number(left.folder) ||
    left.display_name.localeCompare(right.display_name));
  roots.forEach(annotate);
  return roots;
}

export function descendantFiles(node: PickerNode): string[] {
  return node.folder ? node.children.flatMap(descendantFiles) : [node.id];
}

/**
 * Choose a compact, deterministic initial view. Root folders are opened first,
 * then eligible branches are expanded breadth-first so deep leaves cannot
 * consume the whole preview before sibling folders are visible.
 */
export function initiallyExpanded(
  roots: PickerNode[],
  maxVisibleNodes = AUTO_EXPANSION_NODE_LIMIT,
  maxDepth = AUTO_EXPANSION_DEPTH_LIMIT,
): string[] {
  const expanded: string[] = [];
  const queue: Array<{ node: PickerNode; depth: number }> = [];
  let visibleNodes = roots.length;

  for (const root of roots) {
    root.autoCollapsedReason = undefined;
    if (!root.folder) continue;
    expanded.push(root.id);
    visibleNodes += root.children.length;
    queue.push(...root.children.filter((child) => child.folder).map((node) => ({ node, depth: 1 })));
  }

  while (queue.length) {
    const entry = queue.shift()!;
    const { node, depth } = entry;
    node.autoCollapsedReason = undefined;
    if (depth >= maxDepth) {
      node.autoCollapsedReason = "depth limit";
      continue;
    }
    if (node.directChildCount > AUTO_EXPANSION_CHILD_LIMIT) {
      node.autoCollapsedReason = "large branch";
      continue;
    }
    if (visibleNodes + node.children.length > maxVisibleNodes) {
      node.autoCollapsedReason = "preview limit";
      continue;
    }
    expanded.push(node.id);
    visibleNodes += node.children.length;
    queue.push(...node.children.filter((child) => child.folder).map((child) => ({ node: child, depth: depth + 1 })));
  }
  return expanded;
}

export function allFileIds(nodes: PickerNode[]): string[] {
  return nodes.flatMap((node) => descendantFiles(node));
}
