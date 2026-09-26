// The tab strip at the top of a tabbed page (Connections, Captchas): arrow
// keys move between tabs, only the selected tab is in the Tab order, and each
// tab names the panel it controls (`${idPrefix}-panel-${id}`).
import React, { useRef } from "react";

export interface PageTab<T extends string> {
  id: T;
  label: string;
  /** Shown after the label when above zero. */
  count?: number;
  /** A count that needs attention (e.g. captchas waiting). */
  alert?: boolean;
}

export function PageTabs<T extends string>({ tabs, value, onChange, label, idPrefix }: {
  tabs: PageTab<T>[];
  value: T;
  onChange: (id: T) => void;
  label: string;
  idPrefix: string;
}) {
  const refs = useRef<Partial<Record<T, HTMLButtonElement | null>>>({});
  const onKey = (e: React.KeyboardEvent, index: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const next = tabs[(index + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length].id;
    onChange(next);
    refs.current[next]?.focus();
  };
  return (
    <div role="tablist" aria-label={label} className="page-tabs">
      {tabs.map((t, i) => (
        <button
          key={t.id}
          ref={(el) => { refs.current[t.id] = el; }}
          type="button"
          role="tab"
          id={`${idPrefix}-tab-${t.id}`}
          aria-controls={`${idPrefix}-panel-${t.id}`}
          aria-selected={value === t.id}
          tabIndex={value === t.id ? 0 : -1}
          className={`page-tab${value === t.id ? " on" : ""}`}
          onClick={() => onChange(t.id)}
          onKeyDown={(e) => onKey(e, i)}
        >
          {t.label}
          {!!t.count && <span className={`page-tab-count${t.alert ? " alert" : ""}`}>{t.count}</span>}
        </button>
      ))}
    </div>
  );
}
