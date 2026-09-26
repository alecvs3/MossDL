export type CommandContext = {
  selectedCount: number;
  capabilities: Record<string, unknown>;
};

export type CommandId =
  | "delete-selected"
  | "browser-capture"
  | "zoom-in"
  | "zoom-out"
  | "zoom-reset"
  | "linkgrabber"
  | "diagnostics";

export type TransferAction = "pause" | "resume" | "stop" | "restart";

/** Map the presentation status names back to the backend command contract. */
export function normalizeTaskStatus(status: string): string {
  return status === "error" ? "failed" : status === "cancelled" ? "canceled" : status;
}

/** Keep toolbar availability aligned with the backend task-state contract. */
export function canPerformTransferAction(status: string, action: TransferAction): boolean {
  if (action === "pause") return ["resolving", "preflight", "downloading", "queued", "retrying"].includes(status);
  if (action === "resume") return ["paused", "failed", "needs_user"].includes(status);
  if (action === "stop") return ["resolving", "preflight", "downloading", "queued", "retrying", "paused"].includes(status);
  return ["failed", "canceled"].includes(status);
}

type CommandDefinition = { capability?: string; requiresSelection?: boolean; supported?: boolean };

const definitions: Record<CommandId, CommandDefinition> = {
  "delete-selected": { requiresSelection: true },
  "browser-capture": { capability: "browser_capture" },
  "zoom-in": {},
  "zoom-out": {},
  "zoom-reset": {},
  linkgrabber: {},
  diagnostics: {},
};

export function getCommandState(id: CommandId, context: CommandContext): { enabled: boolean; reason?: string } {
  const definition = definitions[id];
  if (definition.requiresSelection && context.selectedCount === 0) {
    return { enabled: false, reason: "Select at least one item" };
  }
  if (definition.capability && context.capabilities[definition.capability] !== true) {
    return { enabled: false, reason: "This capability is not available" };
  }
  return { enabled: definition.supported !== false };
}
