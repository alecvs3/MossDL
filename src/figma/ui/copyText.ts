import { toast } from "./Toasts";

/** Copies text and confirms it; a refused clipboard is reported, not swallowed. */
export function copyText(text: string, what: string): void {
  navigator.clipboard.writeText(text).then(
    () => toast(`Copied ${what}`),
    (error: unknown) => toast(`Couldn't copy ${what}: ${error instanceof Error ? error.message : String(error)}`, { tone: "danger" }),
  );
}
