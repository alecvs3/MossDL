// Short confirmations with an optional action (usually Undo). One host is
// mounted by the app shell; any page calls `toast()`. Messages are also read
// out by screen readers through the host's live region.
import React, { useEffect, useState } from "react";

export interface ToastOptions {
  action?: { label: string; run: () => void };
  tone?: "ok" | "danger";
  /** ms; actions stay a little longer so there is time to reach them. */
  duration?: number;
  /** Runs when the toast times out without its action being used. */
  onExpire?: () => void;
}
interface ToastItem extends ToastOptions { id: number; message: string; leaving?: boolean }

type Listener = (item: ToastItem) => void;
const listeners = new Set<Listener>();
let nextId = 1;
/** Toasts whose action was used: they must not also expire into a commit. */
const acted = new Set<number>();

export function toast(message: string, options: ToastOptions = {}) {
  const item = { id: nextId++, message, ...options };
  listeners.forEach((listener) => listener(item));
}

/** Hide something now; `commit` makes it final when the Undo window closes. */
export function toastUndo(message: string, commit: () => void, undo: () => void) {
  toast(message, { duration: 5000, action: { label: "Undo", run: undo }, onExpire: commit });
}

export function ToastHost() {
  const [items, setItems] = useState<ToastItem[]>([]);

  useEffect(() => {
    const add: Listener = (item) => {
      setItems((current) => [...current.slice(-3), item]);
      const life = item.duration ?? (item.action ? 5000 : 3000);
      window.setTimeout(() => {
        if (!acted.delete(item.id)) item.onExpire?.();
        setItems((current) => current.map((t) => (t.id === item.id ? { ...t, leaving: true } : t)));
      }, life);
      window.setTimeout(() => setItems((current) => current.filter((t) => t.id !== item.id)), life + 250);
    };
    listeners.add(add);
    return () => { listeners.delete(add); };
  }, []);

  return (
    <div className="toast-host" role="status" aria-live="polite">
      {items.map((item) => (
        <div key={item.id} className={`toast${item.tone ? ` ${item.tone}` : ""}${item.leaving ? " leaving" : ""}`}>
          <span>{item.message}</span>
          {item.action && (
            <button
              type="button"
              className="toast-action"
              onClick={() => {
                acted.add(item.id);
                item.action!.run();
                setItems((current) => current.filter((t) => t.id !== item.id));
              }}
            >
              {item.action.label}
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
