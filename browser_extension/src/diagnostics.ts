/** Structured local diagnostics; deliberately excludes URLs, cookies, tokens and payloads. */
export function report(event: string, reason: string): void {
  console.warn(JSON.stringify({component:'mossdl-capture',event,reason}));
}
