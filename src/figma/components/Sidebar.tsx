import React, { useState, useRef, useLayoutEffect } from "react";
import type { Page } from "../types";
import { Icon, ic } from "../icons";

export function Sidebar({
  page,
  setPage,
  captchaCount = 0,
  captureCount = 0,
}: {
  page: Page;
  setPage: (p: Page) => void;
  captchaCount?: number;
  captureCount?: number;
}) {
  const items: { id: Page; icon: string; label: string }[] = [
    { id: "downloads", icon: ic.download, label: "Downloads" },
    { id: "explore", icon: ic.globe, label: "Explore" },
    { id: "captchas", icon: ic.captchas, label: "Captchas" },
    { id: "routes", icon: ic.routes, label: "Connections" },
    { id: "history", icon: ic.history, label: "History" },
    { id: "settings", icon: ic.settings, label: "Settings" },
  ];

  // Dynamically hide labels when the longest one overflows its container.
  // We measure a hidden probe span instead of hardcoding a breakpoint.
  const [showLabels, setShowLabels] = useState(true);
  const probeRef = useRef<HTMLSpanElement>(null);
  const asideRef = useRef<HTMLElement>(null);

  useLayoutEffect(() => {
    const check = () => {
      if (!probeRef.current || !asideRef.current) return;
      // available width for label text = button width minus horizontal padding (px-1.5 = 6px each side)
      const available = asideRef.current.clientWidth - 12;
      setShowLabels(probeRef.current.scrollWidth <= available);
    };
    check();
    const ro = new ResizeObserver(check);
    if (asideRef.current) ro.observe(asideRef.current);
    // also watch the document in case the sidebar itself doesn't resize (e.g. flex parent shrinks)
    ro.observe(document.documentElement);
    return () => ro.disconnect();
  }, []);

  return (
    <aside
      ref={asideRef}
      className="app-sidebar flex flex-col w-[76px] shrink-0 py-2"
      style={{
        borderRight: "1px solid var(--line-06)",
        background: "var(--surface-02)",
      }}
    >
      {/* Hidden probe to measure longest label */}
      <span
        ref={probeRef}
        aria-hidden="true"
        style={{
          position: "absolute",
          visibility: "hidden",
          pointerEvents: "none",
          fontSize: "11px",
          fontWeight: 600,
          letterSpacing: "0.01em",
          whiteSpace: "nowrap",
        }}
      >
        {items.reduce((longest, item) => (item.label.length > longest.length ? item.label : longest), "")}
      </span>

      <div className="flex-1 flex flex-col gap-1 px-1.5">
        {items.map((item) => {
          const active = page === item.id;
          const isCaptchaItem = item.id === "captchas";
          const isCaptureItem = item.id === "explore";
          const showBadge = (isCaptchaItem && captchaCount > 0) || (isCaptureItem && captureCount > 0);
          const badgeCount = isCaptchaItem ? captchaCount : captureCount;
          const badgeClass = isCaptchaItem ? "bg-amber-500 text-black" : "bg-sky-500 text-white";
          const badgeTitle = isCaptchaItem
            ? `${captchaCount} pending verification${captchaCount === 1 ? "" : "s"}`
            : `${captureCount} captured download${captureCount === 1 ? "" : "s"}`;

          return (
            <button
              key={item.id}
              onClick={() => setPage(item.id)}
              className={`nav-item ${active ? "active" : ""} ${isCaptchaItem ? "nav-item-captcha" : ""} ${showBadge ? "has-pending" : ""} relative flex flex-col items-center py-3 rounded-md w-full`}
              style={{ gap: showLabels ? "6px" : "0" }}
              title={item.label}
            >
              <div className="relative">
                <Icon d={item.icon} size={showLabels ? 17 : 19} />
                {showBadge && (
                  <span
                    className={`absolute -top-1.5 -right-2 px-1 min-w-[14px] h-[14px] rounded-full ${badgeClass} text-[8.5px] font-extrabold flex items-center justify-center animate-pulse`}
                    title={badgeTitle}
                  >
                    {badgeCount}
                  </span>
                )}
              </div>
              {showLabels && (
                <span
                  style={{
                    fontSize: "11px",
                    fontWeight: 600,
                    letterSpacing: "0.01em",
                    lineHeight: 1,
                  }}
                >
                  {item.label}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </aside>
  );
}
