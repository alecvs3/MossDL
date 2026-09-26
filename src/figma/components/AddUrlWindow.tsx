// The one place links come in. Paste anything — a file-host link, a direct
// file, a folder, a shortlink, a release page, or a pile of links — and the
// window works out what it is and shows the matching choices.
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  analyzeLink,
  enumerateProvider,
  type IntakeAnalysis,
  type ProviderTreeItem,
} from "../../api";
import { Icon, ic, ProviderIcon, detectProvider, providerLabel } from "../icons";
import { fmtBytes } from "../types";
import { ProviderTreePicker } from "./DownloadModal";
import { SiteIcon } from "../ui/SiteIcon";
import { allFileIds, buildPickerTree } from "../../treePicker";
import { detectMultiPart } from "../../lib/multipart";
import { groupPageLinks, linkFileName, splitPastedLinks, type LinkGroup, type PageLink } from "../../lib/pageLinks";

type AddFn = (
  url: string,
  destination?: string,
  selectedItemIds?: string[],
  duplicateStrategy?: "skip" | "overwrite" | "rename" | "prompt",
  displayName?: string,
  autoExtract?: boolean,
  shouldRefresh?: boolean
) => void | Promise<void>;

export type AddUrlWindowProps = {
  open: boolean;
  onClose: () => void;
  onAdd: AddFn;
  onAdded?: (count: number) => void;
  defaultSavePath: string;
  onPickDirectory?: () => Promise<string | null>;
  autoExtractDefault?: boolean;
  initialUrl?: string;
  lockedUrl?: boolean;
  captureBatchId?: string;
  onDismissCaptured?: () => void;
  /** Opens a page in Explore to see every link and button as a tree. */
  onExplore?: (url: string) => void;
};

type Entry = { url: string; state: "checking" | "done" | "error"; analysis?: IntakeAnalysis; error?: string };
type PlannedAdd = { url: string; displayName?: string; selectedItemIds?: string[]; autoExtract?: boolean };

const KIND_LABEL: Record<IntakeAnalysis["kind"], string> = {
  hoster: "File host",
  folder: "Folder",
  file: "Direct file",
  page: "Web page",
  shortlink: "Shortlink",
  unsupported: "Not supported",
  unknown: "Unchecked link",
};

function errorText(error: unknown): string {
  if (typeof error === "string") return error;
  if (error && typeof error === "object" && "message" in error) return String((error as { message: unknown }).message);
  return "Could not check this link";
}

function leafName(url: string): string | undefined {
  try {
    const leaf = decodeURIComponent(new URL(url).pathname.replace(/\/+$/, "").split("/").pop() || "");
    return leaf.includes(".") ? leaf : undefined;
  } catch {
    return undefined;
  }
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function Section({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <p style={{ fontSize: "10px", fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase", color: "var(--ink-35)" }}>
          {title}
        </p>
        {right}
      </div>
      {children}
    </div>
  );
}

function Notice({ tone, children }: { tone: "info" | "warn" | "error"; children: React.ReactNode }) {
  const palette = {
    info: { bg: "rgba(96,165,250,0.08)", border: "rgba(96,165,250,0.25)", color: "#bfdbfe" },
    warn: { bg: "rgba(251,191,36,0.08)", border: "rgba(251,191,36,0.3)", color: "#fde68a" },
    error: { bg: "rgba(248,113,113,0.08)", border: "rgba(248,113,113,0.3)", color: "#fecaca" },
  }[tone];
  return (
    <div className="rounded-lg px-3 py-2 flex items-start gap-2" style={{ background: palette.bg, border: `1px solid ${palette.border}`, color: palette.color, fontSize: "11px" }}>
      <Icon d={tone === "info" ? ic.link : ic.alertTriangle} size={12} style={{ marginTop: 2, flexShrink: 0 }} />
      <div className="min-w-0">{children}</div>
    </div>
  );
}

/** One file host link, direct file, shortlink or unchecked link. */
function SingleFileView({ analysis, name, onNameChange, autoExtract, onAutoExtract }: {
  analysis: IntakeAnalysis;
  name: string;
  onNameChange: (value: string) => void;
  autoExtract: boolean;
  onAutoExtract: (value: boolean) => void;
}) {
  const provider = detectProvider(analysis.url, analysis.provider_id);
  const multipart = detectMultiPart(name || analysis.url);
  return (
    <div className="space-y-3">
      <div className="rounded-xl p-3 flex items-center gap-3" style={{ background: "var(--surface-03)", border: "1px solid var(--line-07)" }}>
        <ProviderIcon provider={provider} size={26} />
        <div className="flex-1 min-w-0">
          <input
            className="win-input w-full"
            value={name}
            onChange={(event) => onNameChange(event.target.value)}
            placeholder="Name is picked up automatically when the download starts"
            style={{ fontSize: "12px", fontWeight: 500 }}
            aria-label="File name"
          />
          <p className="mt-1 truncate" style={{ fontSize: "10px", color: "var(--ink-40)" }}>
            {analysis.kind === "hoster" ? `${analysis.provider_name || providerLabel(provider)} link` : KIND_LABEL[analysis.kind]}
            {analysis.file?.size ? ` · ${fmtBytes(analysis.file.size / (1024 * 1024))}` : ""}
            {analysis.file?.mime ? ` · ${analysis.file.mime}` : ""}
            {" · "}
            {hostOf(analysis.url)}
          </p>
        </div>
      </div>
      {multipart && (
        <label className="flex items-center gap-2 cursor-pointer" style={{ fontSize: "11px", color: "var(--ink-80)" }}>
          <input type="checkbox" checked={autoExtract} onChange={(event) => onAutoExtract(event.target.checked)} className="accent-blue-400" />
          Part {multipart.part} of a multi-part archive. Extract automatically once every part is in.
        </label>
      )}
    </div>
  );
}

function MirrorPicker({ group, value, onChange }: { group: LinkGroup; value: string; onChange: (host: string) => void }) {
  if (group.mirrors.length < 2) {
    return <span style={{ fontSize: "10px", color: "var(--ink-40)" }}>{group.mirrors[0]?.host}</span>;
  }
  return (
    <div className="flex items-center gap-1 flex-wrap" role="radiogroup" aria-label={`Mirror for ${group.name}`}>
      {group.mirrors.map((mirror) => {
        const active = mirror.host === value;
        return (
          <button
            key={mirror.host}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={(event) => {
              event.stopPropagation();
              onChange(mirror.host);
            }}
            className="flex items-center gap-1 px-1.5 py-0.5 rounded-md transition-colors"
            style={{
              fontSize: "10px",
              background: active ? "var(--accent-soft)" : "var(--surface-05)",
              border: `1px solid ${active ? "var(--accent-line)" : "var(--line-07)"}`,
              color: active ? "var(--accent-light)" : "var(--ink-55)",
            }}
            title={mirror.providerId && mirror.providerId !== "generic" ? `${mirror.host} (supported host)` : mirror.host}
          >
            <ProviderIcon provider={detectProvider(mirror.links[0]?.target_url || "", mirror.providerId)} size={11} />
            {mirror.host}
          </button>
        );
      })}
    </div>
  );
}

function PageView({ analysis, mode, onMode, selected, onToggleGroup, mirrors, onMirror, otherSelected, onToggleOther, autoExtract, onAutoExtract }: {
  analysis: IntakeAnalysis;
  mode: "links" | "page";
  onMode: (mode: "links" | "page") => void;
  selected: Set<string>;
  onToggleGroup: (key: string) => void;
  mirrors: Record<string, string>;
  onMirror: (key: string, host: string) => void;
  otherSelected: Set<string>;
  onToggleOther: (id: string) => void;
  autoExtract: boolean;
  onAutoExtract: (value: boolean) => void;
}) {
  const page = analysis.page!;
  const grouped = useMemo(() => groupPageLinks(page.elements as PageLink[]), [page.elements]);
  const [showOther, setShowOther] = useState(false);
  const hasPackages = grouped.groups.some((group) => group.isPackage);
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 min-w-0">
        {/* Through the engine's favicon cache: a remote <img> would load outside the selected route. */}
        <SiteIcon url={page.url} />
        <div className="min-w-0 flex-1">
          <p className="truncate" style={{ fontSize: "12px", fontWeight: 600, color: "var(--ink-90)" }} title={page.title}>{page.title}</p>
          <p className="truncate" style={{ fontSize: "10px", color: "var(--ink-35)" }}>
            Web page · {grouped.groups.length} download{grouped.groups.length === 1 ? "" : "s"} found · {hostOf(page.url)}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-1 p-1 rounded-lg" style={{ background: "var(--surface-04)" }} role="tablist">
        {([
          ["links", "Download the files on this page"],
          ["page", "Save the page itself (.html)"],
        ] as const).map(([value, label]) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={mode === value}
            onClick={() => onMode(value)}
            className="px-2 py-1.5 transition-colors"
            style={{
              fontSize: "11px",
              fontWeight: 600,
              borderRadius: "var(--radius-control)",
              background: mode === value ? "var(--accent-soft)" : "transparent",
              border: `1px solid ${mode === value ? "var(--accent-line)" : "transparent"}`,
              color: mode === value ? "var(--accent-light)" : "var(--ink-50)",
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {mode === "page" ? (
        <Notice tone="info">
          Saves this page as <span style={{ fontFamily: "'JetBrains Mono',monospace" }}>{page.save_as}</span> instead of downloading what it links to.
        </Notice>
      ) : (
        <>
          {page.error && <Notice tone="warn">Couldn't read this page's links ({page.error}). You can still save the page itself.</Notice>}
          {!page.error && grouped.groups.length === 0 && (
            <Notice tone="warn">No downloads found on this page. Pick links below, or save the page itself.</Notice>
          )}
          {grouped.groups.length > 0 && (
            <Section title="Downloads">
              <div className="space-y-1.5">
                {grouped.groups.map((group) => {
                  const host = mirrors[group.key] ?? group.defaultMirror;
                  const mirror = group.mirrors.find((item) => item.host === host) ?? group.mirrors[0];
                  const checked = selected.has(group.key);
                  return (
                    <div
                      key={group.key}
                      className="rounded-lg px-3 py-2 cursor-pointer transition-colors"
                      onClick={() => onToggleGroup(group.key)}
                      style={{
                        background: checked ? "var(--accent-soft)" : "var(--surface-03)",
                        border: `1px solid ${checked ? "var(--accent-line)" : "var(--line-06)"}`,
                      }}
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <input type="checkbox" checked={checked} readOnly className="accent-blue-400 pointer-events-none" aria-label={group.name} />
                        {group.isPackage && (
                          <span className="px-1.5 rounded shrink-0" style={{ fontSize: "8.5px", fontWeight: 700, background: "rgba(192,132,252,0.15)", color: "#d8b4fe" }}>
                            {mirror.links.length} PARTS
                          </span>
                        )}
                        {group.optional && (
                          <span className="px-1.5 rounded shrink-0" style={{ fontSize: "8.5px", fontWeight: 700, background: "rgba(148,163,184,0.15)", color: "#cbd5e1" }}>
                            OPTIONAL
                          </span>
                        )}
                        <span className="truncate flex-1" style={{ fontSize: "11.5px", fontWeight: 500, color: "var(--ink-90)" }} title={group.name}>
                          {group.name}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 mt-1.5 ml-5">
                        <span style={{ fontSize: "9.5px", color: "var(--ink-35)" }}>
                          {group.mirrors.length > 1 ? `${group.mirrors.length} mirrors:` : "From"}
                        </span>
                        <MirrorPicker group={group} value={mirror.host} onChange={(next) => onMirror(group.key, next)} />
                      </div>
                    </div>
                  );
                })}
              </div>
            </Section>
          )}
          {hasPackages && (
            <label className="flex items-center gap-2 cursor-pointer" style={{ fontSize: "11px", color: "var(--ink-80)" }}>
              <input type="checkbox" checked={autoExtract} onChange={(event) => onAutoExtract(event.target.checked)} className="accent-blue-400" />
              Extract multi-part archives automatically once every part is in
            </label>
          )}
          {grouped.other.length > 0 && (
            <div>
              <button
                type="button"
                onClick={() => setShowOther((value) => !value)}
                className="flex items-center gap-1"
                style={{ fontSize: "10.5px", color: "var(--ink-45)" }}
                aria-expanded={showOther}
              >
                <Icon d={showOther ? ic.chevronDown : ic.chevronRight} size={11} />
                Other links on this page ({grouped.other.length})
              </button>
              {showOther && (
                <div className="mt-1.5 max-h-48 overflow-y-auto rounded-lg" style={{ border: "1px solid var(--line-06)" }}>
                  {grouped.other.map((link) => (
                    <label
                      key={link.id}
                      className="flex items-center gap-2 px-2.5 py-1.5 cursor-pointer hover:bg-white/[0.03]"
                      style={{ opacity: link.unsupported ? 0.45 : 1 }}
                      title={link.unsupported ? "Magnet and torrent links aren't supported" : link.target_url}
                    >
                      <input
                        type="checkbox"
                        disabled={link.unsupported}
                        checked={otherSelected.has(link.id)}
                        onChange={() => onToggleOther(link.id)}
                        className="accent-blue-400"
                      />
                      <span className="truncate flex-1" style={{ fontSize: "10.5px", color: "var(--ink-80)" }}>{link.text || link.target_url}</span>
                      <span className="shrink-0" style={{ fontSize: "9.5px", color: "var(--ink-30)" }}>{link.host || "magnet"}</span>
                    </label>
                  ))}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function BatchView({ entries, unchecked, onToggle, onOpen }: {
  entries: Entry[];
  unchecked: Set<string>;
  onToggle: (url: string) => void;
  onOpen: (url: string) => void;
}) {
  return (
    <Section title={`${entries.length} links`}>
      <div className="rounded-lg overflow-hidden divide-y divide-[var(--line-05)]" style={{ border: "1px solid var(--line-06)" }}>
        {entries.map((entry) => {
          const analysis = entry.analysis;
          const blocked = analysis?.kind === "unsupported" || analysis?.kind === "page";
          const checked = !blocked && entry.state !== "error" && !unchecked.has(entry.url);
          const provider = detectProvider(analysis?.url || entry.url, analysis?.provider_id);
          const pageCount = analysis?.page ? groupPageLinks(analysis.page.elements as PageLink[]).groups.length : 0;
          return (
            <div key={entry.url} className="flex items-center gap-2 px-2.5 py-2">
              <input
                type="checkbox"
                checked={checked}
                disabled={blocked || entry.state !== "done"}
                onChange={() => onToggle(entry.url)}
                className="accent-blue-400"
                aria-label={entry.url}
              />
              {entry.state === "checking" ? (
                <span className="spin" style={{ color: "var(--ink-40)" }}><Icon d={ic.refreshCw} size={12} /></span>
              ) : (
                <ProviderIcon provider={provider} size={14} />
              )}
              <div className="min-w-0 flex-1">
                <p className="truncate" style={{ fontSize: "11px", color: "var(--ink-85)" }} title={entry.url}>
                  {analysis?.file?.name || leafName(analysis?.url || entry.url) || entry.url}
                </p>
                <p className="truncate" style={{ fontSize: "9.5px", color: entry.state === "error" || analysis?.kind === "unsupported" ? "var(--danger)" : "var(--ink-35)" }}>
                  {entry.state === "checking"
                    ? "Checking…"
                    : entry.state === "error"
                    ? entry.error
                    : analysis?.kind === "page"
                    ? `Web page · ${pageCount} download${pageCount === 1 ? "" : "s"} found`
                    : analysis?.kind === "unsupported"
                    ? analysis.message
                    : `${analysis?.kind === "hoster" ? analysis.provider_name : KIND_LABEL[analysis!.kind]} · ${hostOf(analysis!.url)}`}
                </p>
              </div>
              {analysis?.kind === "page" && (
                <button type="button" onClick={() => onOpen(entry.url)} className="shrink-0 px-2 py-0.5 rounded-md hover:bg-[var(--surface-10)]" style={{ fontSize: "10.5px", color: "#93c5fd" }}>
                  Review →
                </button>
              )}
            </div>
          );
        })}
      </div>
    </Section>
  );
}

export function AddUrlWindow({
  open,
  onClose,
  onAdd,
  onAdded,
  defaultSavePath,
  onPickDirectory,
  autoExtractDefault = true,
  initialUrl,
  lockedUrl = false,
  captureBatchId,
  onDismissCaptured,
  onExplore,
}: AddUrlWindowProps) {
  const [text, setText] = useState("");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [savePath, setSavePath] = useState(defaultSavePath);
  const [name, setName] = useState("");
  const [nameEdited, setNameEdited] = useState(false);
  const [autoExtract, setAutoExtract] = useState(autoExtractDefault);
  const [pageMode, setPageMode] = useState<"links" | "page">("links");
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set());
  const [mirrorChoice, setMirrorChoice] = useState<Record<string, string>>({});
  const [otherSelected, setOtherSelected] = useState<Set<string>>(new Set());
  const [unchecked, setUnchecked] = useState<Set<string>>(new Set());
  const [treeItems, setTreeItems] = useState<ProviderTreeItem[]>([]);
  const [treeSelected, setTreeSelected] = useState<string[]>([]);
  const [treeState, setTreeState] = useState<{ loading: boolean; error: string | null }>({ loading: false, error: null });
  const [submitting, setSubmitting] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const requestRef = useRef(0);

  const links = useMemo(() => splitPastedLinks(text), [text]);
  const linksKey = links.join("\n");

  // Fresh window each time it opens, pre-filled with initialUrl if given,
  // or pre-filled from clipboard.
  const [pastedFromClipboard, setPastedFromClipboard] = useState(false);
  useEffect(() => {
    if (!open) return;
    setEntries([]);
    setSavePath(defaultSavePath);
    setSubmitting(false);
    setPastedFromClipboard(false);

    if (initialUrl) {
      setText(initialUrl.trim());
      return;
    }

    setText("");
    let cancelled = false;
    void navigator.clipboard
      ?.readText()
      .then((clip) => {
        if (cancelled) return;
        const links = splitPastedLinks(clip || "");
        if (!links.length) return;
        setText(links.join(String.fromCharCode(10)));
        setPastedFromClipboard(true);
      })
      .catch(() => {
        // Clipboard permission is not required for the window to work.
      });
    return () => {
      cancelled = true;
    };
  }, [open, defaultSavePath, initialUrl]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Check every pasted link shortly after typing stops.
  useEffect(() => {
    const request = ++requestRef.current;
    setName("");
    setNameEdited(false);
    setPageMode("links");
    setSelectedGroups(new Set());
    setMirrorChoice({});
    setOtherSelected(new Set());
    setUnchecked(new Set());
    setTreeItems([]);
    setTreeSelected([]);
    setTreeState({ loading: false, error: null });
    if (!links.length) {
      setEntries([]);
      return;
    }
    setEntries(links.map((url) => ({ url, state: "checking" })));
    const timer = window.setTimeout(() => {
      links.forEach((url) => {
        analyzeLink(url)
          .then((analysis) => {
            if (request !== requestRef.current) return;
            setEntries((current) => current.map((entry) => (entry.url === url ? { url, state: "done", analysis } : entry)));
          })
          .catch((error) => {
            if (request !== requestRef.current) return;
            setEntries((current) => current.map((entry) => (entry.url === url ? { url, state: "error", error: errorText(error) } : entry)));
          });
      });
    }, 350);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linksKey]);

  const single = entries.length === 1 ? entries[0] : null;
  const analysis = single?.state === "done" ? single.analysis : undefined;

  // Defaults once a single link has been understood.
  useEffect(() => {
    if (!analysis) return;
    if (analysis.kind === "page" && analysis.page) {
      const grouped = groupPageLinks(analysis.page.elements as PageLink[]);
      setSelectedGroups(new Set(grouped.groups.filter((group) => !group.optional).map((group) => group.key)));
      // A web page goes to Explore by default (the primary button); saving its
      // HTML is only ever an explicit choice in the page view.
    }
    if (!nameEdited) setName(analysis.file?.name || leafName(analysis.url) || "");
    if (analysis.kind === "folder") {
      const request = requestRef.current;
      setTreeState({ loading: true, error: null });
      enumerateProvider(analysis.url)
        .then((items) => {
          if (request !== requestRef.current) return;
          const valid = items.filter((item) => item && typeof item.display_name === "string");
          setTreeItems(valid);
          setTreeSelected(allFileIds(buildPickerTree(valid)));
          setTreeState({ loading: false, error: null });
        })
        .catch((error) => {
          if (request === requestRef.current) setTreeState({ loading: false, error: errorText(error) });
        });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [analysis]);

  const plan = useMemo<PlannedAdd[]>(() => {
    if (entries.length > 1) {
      return entries
        .filter((entry) => entry.state === "done" && entry.analysis && !["unsupported", "page"].includes(entry.analysis.kind) && !unchecked.has(entry.url))
        .map((entry) => ({ url: entry.analysis!.url, autoExtract }));
    }
    if (!analysis) {
      // A link that couldn't be checked can still be handed to the engine.
      return single?.state === "error" ? [{ url: single.url, autoExtract }] : [];
    }
    switch (analysis.kind) {
      case "unsupported":
        return [];
      case "folder":
        if (treeItems.length && !treeSelected.length) return [];
        return [{ url: analysis.url, selectedItemIds: treeItems.length ? treeSelected : undefined, autoExtract }];
      case "page": {
        const page = analysis.page!;
        if (pageMode === "page") return [{ url: page.url, displayName: page.save_as }];
        const grouped = groupPageLinks(page.elements as PageLink[]);
        const adds: PlannedAdd[] = [];
        for (const group of grouped.groups) {
          if (!selectedGroups.has(group.key)) continue;
          const host = mirrorChoice[group.key] ?? group.defaultMirror;
          const mirror = group.mirrors.find((item) => item.host === host) ?? group.mirrors[0];
          for (const link of mirror.links) adds.push({ url: link.target_url, displayName: linkFileName(link), autoExtract });
        }
        for (const link of grouped.other) {
          if (otherSelected.has(link.id)) adds.push({ url: link.target_url, displayName: linkFileName(link) });
        }
        return adds;
      }
      default:
        // Only override the engine's filename when the user typed one.
        return [{ url: analysis.url, displayName: nameEdited && name.trim() ? name.trim() : undefined, autoExtract }];
    }
  }, [entries, unchecked, analysis, single, treeItems.length, treeSelected, pageMode, selectedGroups, mirrorChoice, otherSelected, name, nameEdited, autoExtract]);

  const checking = entries.some((entry) => entry.state === "checking");
  const actionLabel = (() => {
    if (analysis?.kind === "page" && pageMode === "page") return "Save page";
    if (plan.length > 1) return `Download ${plan.length} files`;
    if (single?.state === "error") return "Add anyway";
    return "Download";
  })();

  // A web page that is not a file, a file host or a shortlink to one: Explore it.
  const explorePage = Boolean(onExplore && analysis?.kind === "page" && analysis.page);
  const bringToExplore = () => {
    if (!analysis?.page || !onExplore) return;
    onExplore(analysis.page.url);
    onClose();
  };

  const submit = async () => {
    if (!plan.length || submitting) return;
    setSubmitting(true);
    try {
      for (let i = 0; i < plan.length; i++) {
        const item = plan[i];
        const isLast = i === plan.length - 1;
        await onAdd(item.url, savePath, item.selectedItemIds, undefined, item.displayName, item.autoExtract, isLast);
      }
      onDismissCaptured?.();
      onAdded?.(plan.length);
      onClose();
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;

  return (
    <div className="modal-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="modal-panel flex flex-col" role="dialog" aria-modal="true" aria-label="Add download" style={{ width: 640 }}>
        <div className="dlg-header shrink-0">
          <span className="dlg-header-icon" style={lockedUrl ? { background: "rgba(14, 165, 233, 0.2)", color: "#38bdf8" } : undefined}>
            <Icon d={lockedUrl ? ic.sparkles : ic.plus} size={15} />
          </span>
          <div className="flex-1 min-w-0">
            <h2 className="dlg-title">{lockedUrl ? "Review Captured Download" : "Add download"}</h2>
            <p className="dlg-subtitle">{lockedUrl ? "Review items, folder tree, and destination before queuing" : "Links, file hosts, folders and release pages"}</p>
          </div>
          <button type="button" onClick={onClose} title="Close (Esc)" aria-label="Close" className="dlg-close">
            <Icon d={ic.close} size={15} />
          </button>
        </div>

        <div className="px-5 pt-4 shrink-0">
          <div className="flex items-center justify-between mb-1">
            <label className="dlg-label" htmlFor="add-url-input">
              {lockedUrl ? "Captured URL" : "Link"}
            </label>
            {lockedUrl && (
              <span className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold tracking-wide" style={{ background: "rgba(14, 165, 233, 0.15)", border: "1px solid rgba(14, 165, 233, 0.35)", color: "#38bdf8" }}>
                <Icon d={ic.sparkles} size={10} />
                Browser Intercept
              </span>
            )}
          </div>
          <textarea
            id="add-url-input"
            ref={inputRef}
            readOnly={lockedUrl}
            autoFocus={!lockedUrl}
            value={text}
            onChange={(event) => !lockedUrl && setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && explorePage && !checking) {
                event.preventDefault();
                bringToExplore();
              } else if (event.key === "Enter" && !event.shiftKey && plan.length && !checking) {
                event.preventDefault();
                void submit();
              }
            }}
            rows={links.length > 1 ? 4 : 2}
            placeholder={lockedUrl ? "Captured download link" : "Paste a link, several links, or a page with downloads on it"}
            className="dlg-input"
            spellCheck={false}
            aria-label="Links to download"
            style={lockedUrl ? { background: "var(--surface-03)", color: "var(--ink-70)", cursor: "default" } : undefined}
          />
          {/* One status line, always present, so nothing below it shifts. */}
          <div className="dlg-status mt-1.5">
            {checking ? (
              <>
                <span className="spin"><Icon d={ic.refreshCw} size={11} /></span>
                Checking {single ? hostOf(single.url) : `${links.length} links`}…
              </>
            ) : pastedFromClipboard ? (
              <>
                <Icon d={ic.clipboardList} size={11} />
                Pasted from your clipboard
                <button
                  type="button"
                  className="underline"
                  style={{ color: "var(--accent-light)" }}
                  onClick={() => {
                    setText("");
                    setPastedFromClipboard(false);
                    inputRef.current?.focus();
                  }}
                >
                  clear
                </button>
              </>
            ) : links.length > 1 ? (
              <>
                <Icon d={ic.link} size={11} />
                {links.length} links found
              </>
            ) : null}
          </div>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto px-5 py-3 space-y-3">
          {!entries.length && (
            <div className="dlg-card">
              {[
                [ic.download, "File hosts", "DataNodes, Mega, Gofile, Pixeldrain… one file or a whole folder"],
                [ic.globe, "Release pages", "Multi-part releases: finds every part and picks one mirror"],
                [ic.link, "Direct files and shortlinks", "Shortlinks are unwrapped before anything downloads"],
                [ic.clipboardList, "Many links at once", "Paste a list and add them all in one go"],
              ].map(([icon, title, body]) => (
                <div key={title} className="dlg-card-row">
                  <span className="dlg-card-icon"><Icon d={icon} size={13} /></span>
                  <div className="min-w-0">
                    <p className="dlg-card-title">{title}</p>
                    <p className="dlg-card-sub">{body}</p>
                  </div>
                </div>
              ))}
            </div>
          )}

          {single?.state === "error" && (
            <Notice tone="warn">Couldn't check this link ({single.error}). You can still add it and the engine will try.</Notice>
          )}

          {analysis?.via_shortlink && (
            <Notice tone="info">
              Unwrapped from {hostOf(analysis.via_shortlink.url)} in {analysis.via_shortlink.hops} step{analysis.via_shortlink.hops === 1 ? "" : "s"}.
            </Notice>
          )}
          {analysis?.redirected_from && <Notice tone="info">Redirected from {hostOf(analysis.redirected_from)}.</Notice>}
          {(analysis?.kind === "unsupported" || analysis?.kind === "shortlink" || analysis?.kind === "unknown") && analysis.message && (
            <Notice tone={analysis.kind === "unsupported" ? "error" : "warn"}>{analysis.message}</Notice>
          )}

          {analysis && ["hoster", "file", "shortlink", "unknown"].includes(analysis.kind) && (
            <SingleFileView
              analysis={analysis}
              name={name}
              onNameChange={(value) => {
                setName(value);
                setNameEdited(true);
              }}
              autoExtract={autoExtract}
              onAutoExtract={setAutoExtract}
            />
          )}

          {analysis?.kind === "folder" && (
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <ProviderIcon provider={detectProvider(analysis.url, analysis.provider_id)} size={20} />
                <p style={{ fontSize: "12px", fontWeight: 600, color: "var(--ink-90)" }}>
                  {analysis.provider_name} folder
                </p>
              </div>
              {treeState.loading && (
                <div className="provider-tree-status"><span className="spin"><Icon d={ic.refreshCw} size={11} /></span> Loading files…</div>
              )}
              {treeState.error && <Notice tone="warn">Couldn't list this folder ({treeState.error}). Downloading will fetch everything in it.</Notice>}
              {!treeState.loading && treeItems.length > 0 && (
                <ProviderTreePicker items={treeItems} selectedIds={treeSelected} onSelectionChange={setTreeSelected} />
              )}
            </div>
          )}

          {analysis?.kind === "page" && analysis.page && (
            <PageView
              analysis={analysis}
              mode={pageMode}
              onMode={setPageMode}
              selected={selectedGroups}
              onToggleGroup={(key) =>
                setSelectedGroups((current) => {
                  const next = new Set(current);
                  if (next.has(key)) next.delete(key);
                  else next.add(key);
                  return next;
                })
              }
              mirrors={mirrorChoice}
              onMirror={(key, host) => setMirrorChoice((current) => ({ ...current, [key]: host }))}
              otherSelected={otherSelected}
              onToggleOther={(id) =>
                setOtherSelected((current) => {
                  const next = new Set(current);
                  if (next.has(id)) next.delete(id);
                  else next.add(id);
                  return next;
                })
              }
              autoExtract={autoExtract}
              onAutoExtract={setAutoExtract}
            />
          )}

          {entries.length > 1 && (
            <BatchView
              entries={entries}
              unchecked={unchecked}
              onToggle={(url) =>
                setUnchecked((current) => {
                  const next = new Set(current);
                  if (next.has(url)) next.delete(url);
                  else next.add(url);
                  return next;
                })
              }
              onOpen={(url) => setText(url)}
            />
          )}
        </div>

        <div className="dlg-footer shrink-0">
          <div className="flex items-center gap-2">
            <label htmlFor="add-save-path" className="shrink-0" style={{ fontSize: "11px", color: "var(--ink-45)" }}>
              Save to
            </label>
            <Icon d={ic.folder} size={13} style={{ color: "var(--ink-35)", flexShrink: 0 }} />
            <input
              id="add-save-path"
              className="dlg-input flex-1 min-w-0"
              value={savePath}
              onChange={(event) => setSavePath(event.target.value)}
              style={{ padding: "6px 9px", fontSize: "11px" }}
              aria-label="Save to folder"
            />
            {onPickDirectory && (
              <button
                type="button"
                onClick={() => void onPickDirectory().then((path) => path && setSavePath(path))}
                className="dlg-button shrink-0"
                style={{ minWidth: 0, height: 28 }}
              >
                Browse…
              </button>
            )}
          </div>
          <div className="dlg-commands">
            <span className="dlg-hint">Enter to add · Esc to close</span>
            <span className="flex-1" />
            <button type="button" onClick={onClose} className="dlg-button">
              Cancel
            </button>
            {explorePage ? (
              <>
                {plan.length > 0 && (
                  <button type="button" onClick={() => void submit()} disabled={checking || submitting || !savePath.trim()} className="dlg-button">
                    {submitting ? "Adding…" : actionLabel}
                  </button>
                )}
                <button type="button" onClick={bringToExplore} className="dlg-button primary"
                  title="See every link, button and mirror on this page, and follow them to the files">
                  Bring to Explore
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => void submit()}
                disabled={!plan.length || checking || submitting || !savePath.trim()}
                className="dlg-button primary"
              >
                {submitting ? "Adding…" : checking ? "Checking…" : actionLabel}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
