import React, { useState, useEffect, useMemo } from "react";
import type { ModalData } from "../types";
import { fmtBytes } from "../types";
import { Icon, ic, ProviderIcon, detectProvider, providerLabel } from "../icons";
import { TypeChip } from "../ui/TypeChip";
import type { PickerNode, ProviderTreeItem } from "../../treePicker";
import { allFileIds, buildPickerTree, descendantFiles, initiallyExpanded } from "../../treePicker";
import { checkDestinationCollision, type DestinationCollisionResult } from "../../api";

export function ProviderTreePicker({
  items,
  selectedIds,
  onSelectionChange,
}: {
  items: ProviderTreeItem[];
  selectedIds: string[];
  onSelectionChange: (ids: string[]) => void;
}) {
  const roots = useMemo(() => buildPickerTree(items), [items]);
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set(initiallyExpanded(roots).map(String))
  );
  const selected = useMemo(() => new Set(selectedIds), [selectedIds]);

  useEffect(() => {
    setExpanded(new Set(initiallyExpanded(roots).map(String)));
  }, [roots]);

  const toggleFiles = (ids: string[]) => {
    if (!ids.length) return;
    const allSelected = ids.every((id) => selected.has(id));
    const next = new Set(selected);
    ids.forEach((id) => (allSelected ? next.delete(id) : next.add(id)));
    onSelectionChange([...next]);
  };
  const toggleExpanded = (id: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const renderNode = (node: PickerNode, depth: number): React.ReactNode => {
    const fileIds = descendantFiles(node);
    const checked = fileIds.length > 0 && fileIds.every((id) => selected.has(id));
    const partiallyChecked = !checked && fileIds.some((id) => selected.has(id));
    const isExpanded = expanded.has(node.id);
    return (
      <React.Fragment key={node.id}>
        <div className="provider-tree-row" style={{ paddingLeft: `${8 + depth * 16}px` }}>
          {node.folder ? (
            <button
              type="button"
              className="provider-tree-expander"
              onClick={() => toggleExpanded(node.id)}
              aria-label={`${isExpanded ? "Collapse" : "Expand"} ${node.display_name}`}
            >
              <Icon d={isExpanded ? ic.chevronDown : ic.chevronRight} size={11} />
            </button>
          ) : (
            <span className="provider-tree-expander-spacer" />
          )}
          <button
            type="button"
            className={`provider-tree-check${checked ? " checked" : ""}`}
            onClick={() => toggleFiles(fileIds)}
            aria-label={`${checked ? "Deselect" : "Select"} ${node.display_name}`}
            aria-pressed={checked}
          >
            {checked ? <Icon d={ic.check} size={9} /> : partiallyChecked ? <span className="provider-tree-mixed" /> : null}
          </button>
          <button
            type="button"
            className="provider-tree-label"
            onClick={() => (node.folder ? toggleExpanded(node.id) : toggleFiles(fileIds))}
          >
            <Icon d={node.folder ? ic.folder : ic.fileDown} size={12} />
            <span className="truncate">{node.display_name}</span>
            {node.folder && <span className="provider-tree-count">{node.fileCount}</span>}
            {!node.folder && node.size != null && (
              <span className="provider-tree-size">{fmtBytes(Number(node.size) / (1024 * 1024))}</span>
            )}
          </button>
        </div>
        {node.folder && isExpanded && node.children.map((child) => renderNode(child, depth + 1))}
      </React.Fragment>
    );
  };

  const fileIds = allFileIds(roots);
  const allSelected = fileIds.length > 0 && fileIds.every((id) => selected.has(id));
  return (
    <div className="provider-tree" aria-label="Provider files">
      <div className="flex items-center justify-between mb-2">
        <p className="provider-tree-heading">Files and folders</p>
        <button type="button" className="provider-tree-select-all" onClick={() => toggleFiles(fileIds)}>
          {allSelected ? "Deselect all" : "Select all"}
        </button>
      </div>
      <div className="provider-tree-list">{roots.map((node) => renderNode(node, 0))}</div>
      <p className="provider-tree-summary">
        {selected.size} of {fileIds.length} files selected
      </p>
    </div>
  );
}

export function DownloadModal({
  data,
  onCancel,
  onStart,
  onPickDirectory,
  treeItems = [],
  treeLoading = false,
  treeError = null,
  autoExtractDefault = true,
}: {
  data: ModalData;
  onCancel: () => void;
  onStart: (d: ModalData) => void;
  onPickDirectory?: () => Promise<string | null>;
  treeItems?: ProviderTreeItem[];
  treeLoading?: boolean;
  treeError?: string | null;
  autoExtractDefault?: boolean;
}) {
  const [local, setLocal] = useState<ModalData>(data);
  const [selectedTreeIds, setSelectedTreeIds] = useState<string[]>([]);
  const [collision, setCollision] = useState<DestinationCollisionResult | null>(null);
  const [dupChoice, setDupChoice] = useState<"copy" | "overwrite" | null>(null);

  // Multi-part package detection
  const detectedPackage = useMemo(() => {
    const name = local.resolvedName.trim();
    // Match: Game.part01.rar, Game.part1.7z, Game.part001.zip, Game.001, Game.r00, Game.z01
    const mPart = name.match(/^(.+?)\.part\d+\.(rar|7z|zip)$/i);
    if (mPart) return { base: mPart[1], ext: mPart[2].toLowerCase(), isMultipart: true };
    const mSplit = name.match(/^(.+?)\.\d{3}$/i);
    if (mSplit) return { base: mSplit[1], ext: "split", isMultipart: true };
    const mRarLegacy = name.match(/^(.+?)\.r\d{2,}$/i);
    if (mRarLegacy) return { base: mRarLegacy[1], ext: "rar", isMultipart: true };
    const mZipLegacy = name.match(/^(.+?)\.z\d{2,}$/i);
    if (mZipLegacy) return { base: mZipLegacy[1], ext: "zip", isMultipart: true };
    return null;
  }, [local.resolvedName]);

  const [createSubfolder, setCreateSubfolder] = useState<boolean>(() => Boolean(detectedPackage?.isMultipart));
  const [subfolderName, setSubfolderName] = useState<string>(() => detectedPackage?.base || "");
  const [autoExtract, setAutoExtract] = useState<boolean>(data.autoExtract ?? autoExtractDefault);

  // Sync subfolder name when detectedPackage updates
  useEffect(() => {
    if (detectedPackage?.base) {
      setSubfolderName(detectedPackage.base);
      setCreateSubfolder(true);
    }
  }, [detectedPackage?.base]);

  const treeFiles = useMemo(() => allFileIds(buildPickerTree(treeItems)), [treeItems]);
  useEffect(() => {
    if (treeFiles.length) setSelectedTreeIds(treeFiles);
  }, [treeFiles]);

  const effectiveSavePath = useMemo(() => {
    const base = local.savePath.trim();
    if (createSubfolder && subfolderName.trim()) {
      const sep = base.includes("/") ? "/" : "\\";
      const cleanSub = subfolderName.trim().replace(/[<>:"/\\|?*]/g, "_");
      return base.endsWith(sep) ? `${base}${cleanSub}` : `${base}${sep}${cleanSub}`;
    }
    return base;
  }, [local.savePath, createSubfolder, subfolderName]);

  useEffect(() => {
    let active = true;
    const savePath = local.savePath.trim();
    const resolvedName = local.resolvedName.trim();
    if (!savePath || !resolvedName) {
      setCollision(null);
      return;
    }
    const timer = setTimeout(() => {
      checkDestinationCollision(savePath, resolvedName, treeItems.length ? selectedTreeIds : undefined)
        .then((res) => {
          if (active) {
            setCollision(res);
            if (!res.exists) setDupChoice(null);
          }
        })
        .catch(() => {
          if (active) setCollision(null);
        });
    }, 150);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [local.savePath, local.resolvedName, selectedTreeIds, treeItems.length]);

  const toggleCandidate = (id: string) =>
    setLocal((p) => ({
      ...p,
      candidates: p.candidates.map((c) => (c.id === id ? { ...c, selected: !c.selected } : c)),
    }));
  const allSelected = local.candidates.every((c) => c.selected);
  const toggleAll = () =>
    setLocal((p) => ({
      ...p,
      candidates: p.candidates.map((c) => ({ ...c, selected: !allSelected })),
    }));

  return (
    <div className="modal-overlay">
      <div className="modal-panel">
        {/* header */}
        <div
          className="flex items-center gap-3 px-5 py-4 shrink-0"
          style={{ borderBottom: "1px solid var(--line-07)" }}
        >
          <ProviderIcon provider={local.provider} size={22} />
          <div className="flex-1 min-w-0">
            <p
              style={{
                fontSize: "10px",
                color: "var(--ink-40)",
                marginBottom: "2px",
                letterSpacing: "0.04em",
              }}
            >
              {providerLabel(local.provider)} · Inspect before downloading
            </p>
            <input
              className="win-input w-full"
              value={local.resolvedName}
              onChange={(e) => setLocal((p) => ({ ...p, resolvedName: e.target.value }))}
              style={{
                fontSize: "12px",
                fontWeight: 500,
                padding: "4px 8px",
                background: "var(--surface-05)",
              }}
            />
          </div>
          <button
            onClick={onCancel}
            title="Close dialog (Esc)"
            style={{ color: "var(--ink-30)", flexShrink: 0, padding: "4px", borderRadius: "4px" }}
            onMouseEnter={(e) => {
              e.currentTarget.style.color = "var(--ink-90)";
              e.currentTarget.style.background = "var(--surface-08)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.color = "var(--ink-30)";
              e.currentTarget.style.background = "transparent";
            }}
          >
            <Icon d={ic.close} size={15} />
          </button>
        </div>

        <div className="overflow-y-auto flex-1 px-5 py-3 space-y-4">
          {/* file info strip */}
          <div className="flex items-center justify-between gap-3 text-xs">
            <div className="flex items-center gap-2.5 min-w-0 overflow-hidden">
              <TypeChip type={local.resolvedName.split(".").pop()?.toUpperCase() ?? "?"} />
              {local.size && (
                <span
                  style={{
                    fontSize: "11px",
                    color: "var(--ink-70)",
                    fontFamily: "'JetBrains Mono',monospace",
                  }}
                >
                  {fmtBytes(local.size)}
                </span>
              )}
              {local.mime && local.mime !== "unknown" && (
                <span
                  className="truncate"
                  title={local.mime}
                  style={{
                    fontSize: "10px",
                    color: "var(--ink-45)",
                    fontFamily: "'JetBrains Mono',monospace",
                    maxWidth: "180px",
                  }}
                >
                  {local.mime}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {local.confidence > 0 && (
                <span
                  className="flex items-center gap-1 px-2 py-0.5 rounded-full"
                  style={{
                    background: "rgba(77,166,245,0.15)",
                    color: "var(--accent-light)",
                    fontSize: "9.5px",
                    fontWeight: 600,
                    border: "1px solid rgba(77,166,245,0.25)",
                  }}
                >
                  <Icon d={ic.shield} size={10} /> {local.confidence}% confidence
                </span>
              )}
              {local.redirects > 0 && (
                <span style={{ fontSize: "9.5px", color: "var(--ink-35)" }}>
                  {local.redirects} redirect{local.redirects !== 1 ? "s" : ""}
                </span>
              )}
            </div>
          </div>

          {/* candidates */}
          {local.candidates.length > 0 && (
            <div>
              <div className="flex items-center justify-between mb-2">
                <p
                  style={{
                    fontSize: "10px",
                    fontWeight: 600,
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    color: "var(--ink-30)",
                  }}
                >
                  Format / Quality
                </p>
                <button
                  onClick={toggleAll}
                  style={{ fontSize: "9.5px", color: "var(--accent-light)", fontWeight: 500 }}
                >
                  {allSelected ? "Deselect all" : "Select all"}
                </button>
              </div>
              <div className="space-y-1">
                {local.candidates.map((c) => (
                  <label
                    key={c.id}
                    className="flex items-center gap-2.5 px-3 py-2 rounded-lg cursor-pointer transition-colors"
                    style={{
                      background: c.selected ? "rgba(0,120,212,0.12)" : "rgba(255,255,255,0.03)",
                      border: c.selected
                        ? "1px solid rgba(77,166,245,0.25)"
                        : "1px solid rgba(255,255,255,0.06)",
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={c.selected}
                      onChange={() => toggleCandidate(c.id)}
                      className="accent-blue-400"
                    />
                    <span style={{ fontSize: "11px", flex: 1, color: "var(--ink-80)" }}>
                      {c.name}
                    </span>
                    <TypeChip type={c.type} />
                    <span
                      style={{
                        fontSize: "10px",
                        color: "var(--ink-40)",
                        fontFamily: "'JetBrains Mono',monospace",
                        flexShrink: 0,
                      }}
                    >
                      {fmtBytes(c.size)}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}

          {treeLoading && (
            <div className="provider-tree-status">
              <span className="spin">
                <Icon d={ic.refreshCw} size={11} />
              </span>{" "}
              Loading provider files…
            </div>
          )}
          {treeError && (
            <p className="provider-tree-status error" role="alert">
              {treeError}
            </p>
          )}
          {!treeLoading && treeItems.length > 0 && (
            <ProviderTreePicker
              items={treeItems}
              selectedIds={selectedTreeIds}
              onSelectionChange={setSelectedTreeIds}
            />
          )}

          {/* save path */}
          <div>
            <p
              style={{
                fontSize: "10px",
                fontWeight: 600,
                letterSpacing: "0.08em",
                textTransform: "uppercase",
                color: "var(--ink-30)",
                marginBottom: "6px",
              }}
            >
              Save to
            </p>
            <div className="flex items-center gap-2">
              <input
                type="text"
                className="win-input flex-1"
                value={local.savePath}
                onChange={(e) => setLocal((p) => ({ ...p, savePath: e.target.value }))}
                style={{ fontSize: "11px" }}
              />
              <button
                type="button"
                onClick={() => {
                  const pick = onPickDirectory?.();
                  if (pick)
                    void pick.then((path) => {
                      if (path) setLocal((current) => ({ ...current, savePath: path }));
                    });
                }}
                className="w-8 h-8 rounded-md flex items-center justify-center shrink-0 transition-colors"
                style={{
                  background: "var(--surface-08)",
                  color: "var(--ink-50)",
                  border: "1px solid var(--line-09)",
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-12)")}
                onMouseLeave={(e) => (e.currentTarget.style.background = "var(--surface-08)")}
              >
                <Icon d={ic.folder} size={13} />
              </button>
            </div>
          </div>

          {/* Multi-part package parent folder & auto-extract card */}
          {detectedPackage && (
            <div
              className="rounded-lg p-3 space-y-2.5"
              style={{
                background: "rgba(0, 120, 212, 0.08)",
                border: "1px solid rgba(0, 120, 212, 0.25)",
              }}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Icon d={ic.layers} size={14} style={{ color: "#60a5fa" }} />
                  <span style={{ fontSize: "11px", fontWeight: 600, color: "#93c5fd" }}>
                    Multi-Part Package Detected ({detectedPackage.ext.toUpperCase()})
                  </span>
                </div>
                <span
                  style={{
                    fontSize: "9px",
                    fontWeight: 600,
                    padding: "1px 6px",
                    borderRadius: "3px",
                    background: "rgba(96,165,250,0.2)",
                    color: "#93c5fd",
                  }}
                >
                  Auto-Grouping
                </span>
              </div>

              <div className="space-y-2 pt-0.5">
                <label className="flex items-start gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={createSubfolder}
                    onChange={(e) => setCreateSubfolder(e.target.checked)}
                    className="accent-blue-400 mt-0.5"
                  />
                  <div className="flex-1 min-w-0">
                    <span style={{ fontSize: "11px", color: "var(--ink-90)", fontWeight: 500 }}>
                      Create parent folder for all parts
                    </span>
                    {createSubfolder && (
                      <input
                        type="text"
                        value={subfolderName}
                        onChange={(e) => setSubfolderName(e.target.value)}
                        className="win-input w-full mt-1.5"
                        style={{ fontSize: "11px", padding: "3px 6px" }}
                        placeholder="Folder name"
                      />
                    )}
                  </div>
                </label>

                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={autoExtract}
                    onChange={(e) => setAutoExtract(e.target.checked)}
                    className="accent-blue-400"
                  />
                  <span style={{ fontSize: "11px", color: "var(--ink-90)", fontWeight: 500 }}>
                    Automatically extract & combine files into parent folder when complete
                  </span>
                </label>

                {createSubfolder && (
                  <p
                    className="truncate"
                    style={{
                      fontSize: "9.5px",
                      color: "var(--ink-45)",
                      fontFamily: "'JetBrains Mono',monospace",
                      marginTop: "4px",
                    }}
                    title={effectiveSavePath}
                  >
                    Target: {effectiveSavePath}
                  </p>
                )}
              </div>
            </div>
          )}

          {/* options */}
          <div className="flex items-center gap-4">
            <div className="flex-1">
              <p
                style={{
                  fontSize: "10px",
                  fontWeight: 600,
                  letterSpacing: "0.08em",
                  textTransform: "uppercase",
                  color: "var(--ink-30)",
                  marginBottom: "6px",
                }}
              >
                Queue
              </p>
              <select
                className="win-select w-full"
                value={local.queue}
                onChange={(e) => setLocal((p) => ({ ...p, queue: e.target.value }))}
              >
                <option value="default">Default</option>
                <option value="high">High priority</option>
                <option value="bg">Background</option>
              </select>
            </div>
          </div>

          {/* File existence collision alert */}
          {collision?.exists && (
            <div
              className="rounded-lg p-3 flex flex-col gap-2"
              style={{
                background: "rgba(245, 158, 11, 0.12)",
                border: "1px solid rgba(245, 158, 11, 0.35)",
              }}
            >
              <div className="flex items-start gap-2">
                <Icon d={ic.alertTriangle} size={14} style={{ color: "var(--warning)", flexShrink: 0, marginTop: "1px" }} />
                <div className="flex-1 min-w-0">
                  <p style={{ fontSize: "11px", fontWeight: 600, color: "#fbbf24" }}>
                    This file already exists in the selected directory
                  </p>
                  <p style={{ fontSize: "10px", color: "var(--ink-70)", marginTop: "2px" }}>
                    A file named <span style={{ fontFamily: "'JetBrains Mono',monospace", color: "#fff" }}>{local.resolvedName}</span> already exists. How would you like to handle this?
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-2 mt-1">
                {collision.suggested_name && (
                  <button
                    type="button"
                    onClick={() => {
                      setLocal((p) => ({ ...p, resolvedName: collision.suggested_name! }));
                      setDupChoice("copy");
                    }}
                    className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-semibold transition-colors"
                    style={{
                      background: dupChoice === "copy" ? "var(--accent-light)" : "rgba(255,255,255,0.1)",
                      color: dupChoice === "copy" ? "#000" : "rgba(255,255,255,0.9)",
                      border: "1px solid var(--line-12)",
                    }}
                  >
                    <Icon d={ic.copy} size={11} />
                    Make a Copy ({collision.suggested_name})
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => {
                    setDupChoice("overwrite");
                  }}
                  className="flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium transition-colors"
                  style={{
                    background: dupChoice === "overwrite" ? "rgba(239, 68, 68, 0.25)" : "rgba(255,255,255,0.06)",
                    color: dupChoice === "overwrite" ? "#fca5a5" : "rgba(255,255,255,0.7)",
                    border: dupChoice === "overwrite" ? "1px solid rgba(239, 68, 68, 0.4)" : "1px solid rgba(255,255,255,0.1)",
                  }}
                >
                  <Icon d={ic.refreshCw} size={11} />
                  Overwrite existing
                </button>
              </div>
            </div>
          )}
        </div>

        {/* footer */}
        <div
          className="flex items-center justify-end gap-2 px-5 py-3 shrink-0"
          style={{ borderTop: "1px solid var(--line-07)" }}
        >
          <button
            onClick={onCancel}
            className="px-4 py-1.5 rounded-md transition-colors"
            style={{
              fontSize: "11.5px",
              fontWeight: 500,
              color: "var(--ink-60)",
              background: "var(--surface-08)",
              border: "1px solid var(--line-09)",
            }}
            onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-10)")}
            onMouseLeave={(e) => (e.currentTarget.style.background = "var(--surface-08)")}
          >
            Cancel
          </button>
          <button
            onClick={() =>
              onStart({
                ...local,
                savePath: effectiveSavePath,
                createSubfolder,
                subfolderName: subfolderName.trim(),
                autoExtract,
                selectedItemIds: treeItems.length ? selectedTreeIds : undefined,
                duplicateStrategy:
                  dupChoice === "overwrite"
                    ? "overwrite"
                    : collision?.exists
                    ? "rename"
                    : "skip",
              })
            }
            className="btn-accent px-4 py-1.5 rounded-md"
            style={{ fontSize: "11.5px", fontWeight: 600 }}
          >
            Start Download
          </button>
        </div>
      </div>
    </div>
  );
}
