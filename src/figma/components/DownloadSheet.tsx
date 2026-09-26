import React, { useState } from "react";
import { SiteIcon } from "../ui/SiteIcon";
import type { DownloadWithHistory, Provider } from "../types";
import { fmtBytes, pct, quotaSignal } from "../types";
import { Icon, ic, detectProvider, providerLabel } from "../icons";
import { TypeChip } from "../ui/TypeChip";
import { StatusBadge } from "../ui/StatusBadge";
import { engine } from "../../api";

export function DownloadSheet({
  d,
  defaultSavePath,
  onClose,
  onOpenFolder,
  onNavigate,
}: {
  d: DownloadWithHistory | null;
  defaultSavePath: string;
  onClose: () => void;
  onOpenFolder?: (path: string) => void;
  onNavigate?: (page: any) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [dragStartY, setDragStartY] = useState<number | null>(null);
  const [sha512State, setSha512State] = useState<
    | "idle"
    | "loading"
    | { hash: string; state: string; sizeOk: boolean | null; expectedSize: number | null; observedSize: number | null; reason: string | null }
    | { error: string }
  >("idle");
  const open = d !== null;
  const quota = d ? quotaSignal(d) : null;

  const handlePointerDown = (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    setDragStartY(e.clientY);
  };

  const handlePointerMove = (e: React.PointerEvent) => {
    if (dragStartY !== null) {
      const deltaY = e.clientY - dragStartY;
      if (deltaY > 40) {
        setDragStartY(null);
        onClose();
      }
    }
  };

  const handlePointerUp = () => {
    setDragStartY(null);
  };

  const copyUrl = () => {
    if (!d) return;
    navigator.clipboard.writeText(d.url).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  // Fixed viewBox coordinate space — SVG scales to fill width without distorting text
  const VW = 400,
    VH = 100,
    PAD_L = 34,
    PAD_B = 14,
    PAD_R = 4,
    PAD_T = 4;
  const innerW = VW - PAD_L - PAD_R;
  const innerH = VH - PAD_T - PAD_B;

  const history = d?.speedHistory ?? [];
  const peakSpeed = Math.max(...history, 0);
  // Ensure a sensible minimum scale floor (at least 10 MB/s) so moderate speeds
  // don't appear artificially maxed out to the chart ceiling.
  const maxVal = Math.max(peakSpeed * 1.2, 10);
  const midVal = maxVal / 2;

  const toX = (i: number) => PAD_L + (history.length < 2 ? innerW : (i / (history.length - 1)) * innerW);
  const toY = (v: number) => PAD_T + innerH - (v / maxVal) * innerH;

  const pts = history.map((v, i) => `${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(" ");
  const fillPts =
    history.length >= 2
      ? `${toX(0).toFixed(1)},${(PAD_T + innerH).toFixed(1)} ${pts} ${toX(history.length - 1).toFixed(1)},${(
          PAD_T + innerH
        ).toFixed(1)}`
      : "";

  const provider = d ? detectProvider(d.url, d.provider) : ("direct" as Provider);
  const p = d ? pct(d) : 0;

  const displayDone = d ? (d.size > 0 ? Math.min(d.downloaded, d.size) : d.downloaded) : 0;

  return (
    <div className={`dl-sheet${open ? " dl-sheet-open" : ""}`} onClick={(e) => e.stopPropagation()}>
      {/* drag handle */}
      <div
        className="flex justify-center pt-2.5 pb-1 shrink-0 cursor-grab active:cursor-grabbing select-none"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
        onClick={(e) => {
          e.stopPropagation();
          onClose();
        }}
        title="Drag down or click to close"
      >
        <div style={{ width: 36, height: 4, borderRadius: 2, background: "var(--surface-20)" }} />
      </div>

      {/* header */}
      <div
        className="flex items-center gap-2.5 px-4 pb-2.5 shrink-0"
        style={{ borderBottom: "1px solid var(--line-07)" }}
      >
        {d && <SiteIcon url={d.url} size={14} />}
        {d && <TypeChip type={d.type} />}
        <p className="flex-1 truncate" style={{ fontSize: "11.5px", fontWeight: 700, color: "var(--ink-90)" }}>
          {d?.name ?? ""}
        </p>
        <button
          onClick={onClose}
          className="w-6 h-6 flex items-center justify-center rounded transition-colors"
          style={{ color: "var(--ink-35)", background: "transparent" }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = "var(--surface-08)";
            e.currentTarget.style.color = "var(--ink-70)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = "transparent";
            e.currentTarget.style.color = "var(--ink-35)";
          }}
        >
          <Icon d={ic.close} size={12} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {/* Quota / 429 Recovery Alert Banner — only on real engine quota signals */}
        {d && quota && (
          <div
            className="mx-4 mt-2.5 p-3 rounded-md flex flex-col gap-2"
            style={{
              background: "rgba(245,158,11,0.08)",
              border: "1px solid rgba(245,158,11,0.25)",
            }}
          >
            <div className="flex items-start gap-2">
              <Icon d={ic.alertTriangle} size={14} style={{ color: "var(--warning)", marginTop: "1px", flexShrink: 0 }} />
              <div className="flex-1 min-w-0">
                <div style={{ fontSize: "11px", fontWeight: 700, color: "#fbbf24" }}>
                  Download Quota Reached
                </div>
                <div style={{ fontSize: "9.5px", color: "var(--ink-70)", marginTop: "2px", lineHeight: "1.4" }}>
                  {d.error || "Hoster bandwidth limit reached. Connect an account for this provider or switch your VPN / proxy route to bypass the limit."}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2 pl-5 pt-1">
              <button
                onClick={() => {
                  onClose();
                  onNavigate?.("settings");
                }}
                className="flex items-center gap-1.5 px-2.5 py-1 rounded text-white font-medium transition-colors"
                style={{
                  fontSize: "9.5px",
                  background: "rgba(245,158,11,0.25)",
                  border: "1px solid rgba(245,158,11,0.4)",
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(245,158,11,0.4)")}
                onMouseLeave={(e) => (e.currentTarget.style.background = "rgba(245,158,11,0.25)")}
              >
                <Icon d={ic.userCheck} size={10} />
                Connect Account
              </button>
              <button
                onClick={() => {
                  onClose();
                  onNavigate?.("routes");
                }}
                className="flex items-center gap-1.5 px-2.5 py-1 rounded text-white font-medium transition-colors"
                style={{
                  fontSize: "9.5px",
                  background: "rgba(59,130,246,0.2)",
                  border: "1px solid rgba(59,130,246,0.35)",
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(59,130,246,0.35)")}
                onMouseLeave={(e) => (e.currentTarget.style.background = "rgba(59,130,246,0.2)")}
              >
                <Icon d={ic.routes} size={10} />
                Switch Route / VPN
              </button>
            </div>
          </div>
        )}

        {/* ── Main body: info left + chart right ─────────────── */}
        {d && (
          <div className="flex gap-0 px-4 pt-3 pb-2" style={{ minHeight: "120px" }}>
            {/* Left: info grid */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr",
                gap: "3px 10px",
                alignItems: "center",
                alignContent: "start",
                flexShrink: 0,
                width: "160px",
                paddingRight: "12px",
              }}
            >
              <span
                style={{
                  fontSize: "8.5px",
                  color: "var(--ink-30)",
                  fontWeight: 600,
                  whiteSpace: "nowrap",
                  letterSpacing: "0.04em",
                  textTransform: "uppercase",
                  gridColumn: "1/-1",
                  marginBottom: "2px",
                }}
              >
                Details
              </span>
              {[
                { k: "Source", v: providerLabel(provider) },
                { k: "Added", v: d.added },
                { k: "Size", v: `${fmtBytes(d.size)}` },
                { k: "Done", v: `${fmtBytes(displayDone)} (${p}%)` },
                { k: "Status", v: d.status, isStatus: true },
                { k: "ETA", v: d.status === "downloading" ? d.eta : "—" },
              ].map((row) => (
                <React.Fragment key={row.k}>
                  <span style={{ fontSize: "8.5px", color: "var(--ink-30)", fontWeight: 600, whiteSpace: "nowrap" }}>
                    {row.k}
                  </span>
                  <span
                    style={{
                      fontSize: "9.5px",
                      color: "var(--ink-70)",
                      fontFamily: "'JetBrains Mono',monospace",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {row.isStatus ? (
                      <StatusBadge
                        status={d.status}
                        rawState={d.rawState}
                        detailedStatus={d.detailedStatus} stage={d.stage} stageDetail={d.stageDetail}
                        stageEnteredAt={d.stageEnteredAt}
                        error={d.error}
                        pausedReason={d.pausedReason}
                      />
                    ) : (
                      row.v
                    )}
                  </span>
                </React.Fragment>
              ))}
              {/* save-to */}
              <span style={{ fontSize: "8.5px", color: "var(--ink-30)", fontWeight: 600, whiteSpace: "nowrap" }}>
                Path
              </span>
              <div className="flex items-center gap-1 min-w-0">
                <span
                  style={{
                    fontSize: "9px",
                    color: "var(--ink-50)",
                    fontFamily: "'JetBrains Mono',monospace",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {d.destination || defaultSavePath}
                </span>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    const folder = d.destination || defaultSavePath;
                    if (folder) onOpenFolder?.(folder);
                  }}
                  title="Open folder in File Explorer"
                  style={{ color: "var(--ink-40)", flexShrink: 0, cursor: "pointer", padding: "2px" }}
                  onMouseEnter={(e) => (e.currentTarget.style.color = "var(--accent-light)")}
                  onMouseLeave={(e) => (e.currentTarget.style.color = "var(--ink-40)")}
                >
                  <Icon d={ic.folder} size={11} />
                </button>
              </div>
            </div>

            {/* Right: speed chart fills remaining space */}
            {history.length > 0 && (
              <div
                className="flex-1 flex flex-col min-w-0"
                style={{ borderLeft: "1px solid var(--line-05)", paddingLeft: "12px" }}
              >
                <div className="flex items-center justify-between mb-1.5">
                  <span
                    style={{
                      fontSize: "8.5px",
                      fontWeight: 600,
                      color: "var(--ink-30)",
                      letterSpacing: "0.07em",
                      textTransform: "uppercase",
                    }}
                  >
                    Transfer Speed
                  </span>
                  <span style={{ fontSize: "9px", color: "var(--accent-light)", fontFamily: "'JetBrains Mono',monospace" }}>
                    {history[history.length - 1].toFixed(2)}{" "}
                    <span style={{ fontSize: "7.5px", opacity: 0.6 }}>MB/s</span>
                  </span>
                </div>
                <svg
                  width="100%"
                  viewBox={`0 0 ${VW} ${VH}`}
                  preserveAspectRatio="xMidYMid meet"
                  style={{ display: "block", flex: 1 }}
                >
                  {/* grid lines — 0%, 50%, and 100% */}
                  {[0, 0.5, 1.0].map((f) => (
                    <line
                      key={f}
                      x1={PAD_L}
                      y1={PAD_T + innerH * (1 - f)}
                      x2={PAD_L + innerW}
                      y2={PAD_T + innerH * (1 - f)}
                      stroke="rgba(255,255,255,0.05)"
                      strokeWidth="0.5"
                    />
                  ))}
                  {/* y-axis labels */}
                  <text
                    x={PAD_L - 3}
                    y={PAD_T + innerH}
                    textAnchor="end"
                    fontSize="5"
                    fill="rgba(255,255,255,0.22)"
                    dominantBaseline="middle"
                  >
                    0
                  </text>
                  <text
                    x={PAD_L - 3}
                    y={PAD_T + innerH / 2}
                    textAnchor="end"
                    fontSize="5"
                    fill="rgba(255,255,255,0.22)"
                    dominantBaseline="middle"
                  >
                    {midVal.toFixed(1)}
                  </text>
                  <text
                    x={PAD_L - 3}
                    y={PAD_T + 3}
                    textAnchor="end"
                    fontSize="5"
                    fill="rgba(255,255,255,0.22)"
                    dominantBaseline="middle"
                  >
                    {maxVal.toFixed(0)}
                  </text>
                  <text
                    x={PAD_L + innerW}
                    y={PAD_T + innerH + 10}
                    textAnchor="end"
                    fontSize="4.5"
                    fill="rgba(255,255,255,0.15)"
                  >
                    MB/s
                  </text>
                  {/* area fill */}
                  {fillPts && <polygon points={fillPts} fill="url(#speedGrad)" opacity="0.3" />}
                  {/* line */}
                  {history.length >= 2 && (
                    <polyline
                      points={pts}
                      fill="none"
                      style={{ stroke: "var(--accent-light)" }}
                      strokeWidth="1.5"
                      strokeLinejoin="round"
                      strokeLinecap="round"
                    />
                  )}
                  {/* endpoint dot */}
                  {history.length >= 1 && (
                    <circle cx={toX(history.length - 1)} cy={toY(history[history.length - 1])} r="2.5" style={{ fill: "var(--accent-light)" }} />
                  )}
                  <defs>
                    <linearGradient id="speedGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" style={{ stopColor: "var(--accent-light)" }} />
                      <stop offset="100%" stopOpacity="0" style={{ stopColor: "var(--accent-light)" }} />
                    </linearGradient>
                  </defs>
                </svg>
              </div>
            )}
          </div>
        )}

        {/* SHA-512 file integrity verification */}
        {d && d.status === "completed" && d.destination && (
          <div
            className="flex flex-col gap-1.5 px-4 pb-2 pt-1"
            style={{ borderTop: "1px solid var(--line-05)" }}
          >
            <div className="flex items-center gap-2">
              <Icon d={ic.shield} size={11} style={{ color: "var(--ink-25)", flexShrink: 0 }} />
              <span style={{ fontSize: "8.5px", fontWeight: 600, color: "var(--ink-30)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                File Integrity
              </span>
              <button
                onClick={() => {
                  if (sha512State === "loading") return;
                  // `destination` is the containing folder for multi-part
                  // packages; verify the actual file path instead.
                  const dest = (d.destination || "").replace(/[/\\]+$/, "");
                  const sep = dest.includes("\\") ? "\\" : "/";
                  const hasExtension = /\.[a-z0-9]{1,8}$/i.test(d.name || "");
                  const verifyPath =
                    d.name && hasExtension && !dest.toLowerCase().endsWith(d.name.toLowerCase())
                      ? `${dest}${sep}${d.name}`
                      : dest;
                  setSha512State("loading");
                  engine<{
                    state?: string;
                    observed_checksum?: string | null;
                    expected_size?: number | null;
                    observed_size?: number | null;
                    reason?: string | null;
                    size_ok?: boolean;
                  }>("verify_download", {
                    path: verifyPath,
                    algorithm: "sha512",
                    task_id: d.id,
                  })
                    .then((res) => {
                      const expectedSize = typeof res.expected_size === "number" ? res.expected_size : null;
                      const observedSize = typeof res.observed_size === "number" ? res.observed_size : null;
                      // Prefer the engine's explicit size_ok when present;
                      // otherwise derive it from expected/observed sizes.
                      const sizeOk =
                        typeof res.size_ok === "boolean"
                          ? res.size_ok
                          : expectedSize !== null && observedSize !== null
                          ? expectedSize === observedSize
                          : null;
                      setSha512State({
                        hash: res.observed_checksum || "",
                        state: res.state || "unknown",
                        sizeOk,
                        expectedSize,
                        observedSize,
                        reason: res.reason || null,
                      });
                    })
                    .catch((err: unknown) =>
                      setSha512State({ error: typeof err === "string" ? err : "Verification failed" })
                    );
                }}
                className="ml-auto flex items-center gap-1 px-2 py-0.5 rounded transition-colors"
                style={{
                  fontSize: "9px",
                  fontWeight: 600,
                  background: sha512State === "loading" ? "rgba(255,255,255,0.04)" : "rgba(255,255,255,0.07)",
                  color: sha512State === "loading" ? "rgba(255,255,255,0.3)" : "rgba(255,255,255,0.5)",
                  cursor: sha512State === "loading" ? "wait" : "pointer",
                }}
                disabled={sha512State === "loading"}
                title="Compute SHA-512 checksum of the downloaded file"
              >
                <Icon d={ic.check} size={9} />
                {sha512State === "loading" ? "Computing…" : "Verify SHA-512"}
              </button>
            </div>
            {sha512State !== "idle" && sha512State !== "loading" && (
              <div
                style={{
                  fontSize: "8.5px",
                  fontFamily: "'JetBrains Mono',monospace",
                  wordBreak: "break-all",
                  color: "error" in sha512State
                      ? "var(--danger)"
                      : sha512State.sizeOk === false || sha512State.state === "corrupt"
                      ? "#fbbf24"
                      : sha512State.state === "verified"
                      ? "var(--success)"
                      : "rgba(255,255,255,0.6)",
                  background: "var(--surface-04)",
                  borderRadius: "4px",
                  padding: "4px 6px",
                  lineHeight: 1.5,
                }}
              >
                {"error" in sha512State ? (
                  `Error: ${sha512State.error}`
                ) : (
                  <>
                    <div>{sha512State.hash || "(no checksum returned)"}</div>
                    <div style={{ marginTop: "2px", color: "var(--ink-60)" }}>
                      {sha512State.sizeOk === false
                        ? `Size mismatch — expected ${sha512State.expectedSize ?? "?"} bytes, observed ${sha512State.observedSize ?? "?"} bytes`
                        : sha512State.sizeOk === true
                        ? `Size OK (${sha512State.observedSize} bytes)`
                        : "Size not checked"}
                      {sha512State.reason ? ` · ${sha512State.reason}` : ""}
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        )}

        {/* URL copy row */}
        {d && (
          <div className="flex items-center gap-2 px-4 pb-3 pt-1">
            <Icon d={ic.link} size={11} style={{ color: "var(--ink-25)", flexShrink: 0 }} />
            <span
              className="flex-1 truncate"
              style={{
                fontSize: "9.5px",
                color: "var(--ink-40)",
                fontFamily: "'JetBrains Mono',monospace",
              }}
            >
              {d.url}
            </span>
            <button
              onClick={copyUrl}
              className="flex items-center gap-1 px-2 py-1 rounded transition-colors shrink-0"
              style={{
                fontSize: "9.5px",
                fontWeight: 600,
                background: "var(--surface-08)",
                color: copied ? "var(--success)" : "rgba(255,255,255,0.5)",
              }}
              onMouseEnter={(e) => {
                if (!copied) e.currentTarget.style.background = "var(--surface-12)";
              }}
              onMouseLeave={(e) => {
                if (!copied) e.currentTarget.style.background = "var(--surface-08)";
              }}
            >
              <Icon d={copied ? ic.check : ic.clipboardList} size={10} />
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
