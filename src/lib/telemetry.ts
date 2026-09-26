import { invoke } from "@tauri-apps/api/core";

export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";

export interface LogErrorDetails {
  type: string;
  message: string;
  traceback?: string | null;
}

export interface LogEvent {
  id: number;
  timestamp: string;
  epoch_ms: number;
  level: LogLevel;
  tier: string;
  subsystem: string;
  message: string;
  context?: Record<string, unknown> | null;
  error?: LogErrorDetails | null;
  duration_ms?: number | null;
}

export interface LogQueryOptions {
  since_id?: number;
  min_level?: LogLevel;
  subsystem?: string;
  limit?: number;
  [key: string]: unknown;
}

export interface LogQueryResponse {
  events: LogEvent[];
  count: number;
}

export interface LogExportResponse {
  export_path: string;
}

let isLoggingActive = false;

// A repeating UI error used to send two IPC calls per occurrence, which can
// swamp the engine (every call is serialised) and make the whole app stutter.
// Identical events collapse inside this window and report how many were folded in.
const REPEAT_WINDOW_MS = 3000;
const recentLogs = new Map<string, { at: number; suppressed: number }>();

function repeatSuppression(key: string): { skip: boolean; suppressed: number } {
  const now = Date.now();
  const previous = recentLogs.get(key);
  if (previous && now - previous.at < REPEAT_WINDOW_MS) {
    previous.suppressed += 1;
    return { skip: true, suppressed: previous.suppressed };
  }
  const suppressed = previous?.suppressed ?? 0;
  recentLogs.set(key, { at: now, suppressed: 0 });
  if (recentLogs.size > 200) {
    for (const [entry, value] of recentLogs) {
      if (now - value.at > REPEAT_WINDOW_MS) recentLogs.delete(entry);
    }
  }
  return { skip: false, suppressed };
}

/**
 * Emit a structured log event to the central engine telemetry bus.
 * Uses a recursion lock and non-blocking delivery to guarantee safe execution.
 */
export async function emitLog(
  level: LogLevel | string,
  subsystem: string,
  message: string,
  context?: Record<string, unknown>,
  error?: unknown,
  duration_ms?: number
): Promise<void> {
  if (isLoggingActive) {
    return;
  }
  const repeat = repeatSuppression(`${level}|${subsystem}|${message}`);
  if (repeat.skip) {
    return;
  }
  isLoggingActive = true;
  try {
    let errorObj: LogErrorDetails | undefined = undefined;
    if (error) {
      if (error instanceof Error) {
        errorObj = {
          type: error.name || "Error",
          message: error.message || String(error),
          traceback: error.stack || null,
        };
      } else if (typeof error === "object") {
        const errRecord = error as Record<string, unknown>;
        errorObj = {
          type: String(errRecord.type || errRecord.name || "ObjectError"),
          message: String(errRecord.message || JSON.stringify(error)),
          traceback: typeof errRecord.traceback === "string" ? errRecord.traceback : (typeof errRecord.stack === "string" ? errRecord.stack : null),
        };
      } else {
        errorObj = {
          type: "Error",
          message: String(error),
          traceback: null,
        };
      }
    }

    const payload = {
      level: String(level).toUpperCase(),
      subsystem,
      message: repeat.suppressed > 0 ? `${message} (repeated ${repeat.suppressed}x)` : message,
      context: context || null,
      error: errorObj || null,
      duration_ms: duration_ms ?? null,
      tier: "ui",
    };

    // Forward to Tauri native stderr for instant local visibility
    try {
      await invoke("dev_log", {
        level: payload.level,
        tag: subsystem,
        message,
        details: context ? { ...context, ...(errorObj ? { error: errorObj } : {}) } : errorObj,
      });
    } catch {
      // Ignore Tauri invoke failures during early boot
    }

    // Forward to Engine telemetry bus via engine_request
    try {
      await invoke("engine_request", {
        method: "log_event",
        params: payload,
      });
    } catch {
      // Ignore engine dispatch failure
    }
  } catch (err) {
    console.error("[telemetry] failed to emit log event:", err);
  } finally {
    isLoggingActive = false;
  }
}
