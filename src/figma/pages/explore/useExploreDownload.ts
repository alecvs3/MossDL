// Sending Explore rows to Downloads, each through the engine path that owns it:
// pasted links via the link grabber, browser captures via capture import
// (which refuses duplicates and says so), everything else via add_task.
import { useCallback } from "react";
import type { CaptureImportResult, FileCategory } from "../../../api";
import { toast } from "../../ui/Toasts";
import type { ExploreNode } from "./exploreModel";
import { isDownloadable } from "./exploreView";

export interface DownloadDeps {
  defaultSavePath: string;
  onAddUrl?: (url: string, destination?: string, selectedItemIds?: string[], duplicateStrategy?: "skip" | "overwrite" | "rename" | "prompt", displayName?: string, autoExtract?: boolean) => void;
  onCaptureImport?: (params: { batchId: string; candidateId?: string; candidateIndex: number; destination: string; category?: FileCategory; queueId: string; addAnyway?: boolean }) => Promise<CaptureImportResult>;
  onLinkEnqueue?: (ids: string[]) => Promise<void>;
  onPickDirectory?: () => Promise<string | null>;
  onShowDownloads?: () => void;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function useExploreDownload(deps: DownloadDeps) {
  const download = useCallback(async (nodes: ExploreNode[], opts: { pickFolder?: boolean; addAnyway?: boolean } = {}): Promise<void> => {
    const files = nodes.filter(isDownloadable);
    if (!files.length) return;
    const destination = opts.pickFolder ? await deps.onPickDirectory?.() : undefined;
    if (opts.pickFolder && !destination) return;
    const duplicates: ExploreNode[] = [];
    let added = 0;

    const entryIds = opts.pickFolder || !deps.onLinkEnqueue ? [] : files.flatMap((f) => (f.origin.kind === "entry" ? [f.origin.entry.id] : []));
    if (entryIds.length) { await deps.onLinkEnqueue!(entryIds); added += entryIds.length; }

    for (const f of files) {
      if (f.origin.kind === "entry" && entryIds.includes(f.origin.entry.id)) continue;
      if (f.origin.kind === "capture" && deps.onCaptureImport) {
        const result = await deps.onCaptureImport({ batchId: f.origin.batchId, candidateId: f.origin.candidate.candidate_id, candidateIndex: f.origin.index,
          destination: destination ?? deps.defaultSavePath, queueId: "default", addAnyway: opts.addAnyway });
        if (result.status === "duplicate") duplicates.push(f); else added++;
      } else {
        deps.onAddUrl?.(f.url!, destination ?? undefined, undefined, undefined, f.label, true);
        added++;
      }
    }

    if (added) {
      toast(`Added ${added === 1 && files.length === 1 ? `“${files[0].label}”` : plural(added, "file")} to Downloads`, {
        tone: "ok", action: deps.onShowDownloads ? { label: "Show", run: deps.onShowDownloads } : undefined,
      });
    }
    if (duplicates.length) {
      toast(`${duplicates.length === 1 ? `“${duplicates[0].label}” is` : `${plural(duplicates.length, "file")} are`} already in Downloads`, {
        action: { label: "Add anyway", run: () => void download(duplicates, { addAnyway: true }) },
      });
    }
  }, [deps]);
  return download;
}
