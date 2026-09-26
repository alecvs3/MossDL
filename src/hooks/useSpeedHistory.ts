import { useEffect, useState } from "react";
import type { Task } from "../api";

const MAX_SAMPLES = 48;

export function useSpeedHistory(tasks: Task[]) {
  const [history, setHistory] = useState<Record<string, number[]>>({});

  useEffect(() => {
    setHistory((current) => {
      const next = { ...current };
      for (const task of tasks) {
        const speed = Math.max(0, task.speed_bytes_per_second || 0);
        const samples = next[task.id] || [];
        // The snapshot is the engine telemetry clock (currently one second).
        // Record every observation so a steady transfer still produces a
        // visible line instead of a single endpoint. Keep terminal histories
        // intact when the engine leaves the task in the list.
        const isSampling = ["resolving", "preflight", "downloading", "retrying", "paused", "queued"].includes(task.state);
        if (isSampling || samples.length === 0) next[task.id] = [...samples, speed].slice(-MAX_SAMPLES);
      }
      const activeIds = new Set(tasks.map((task) => task.id));
      for (const id of Object.keys(next)) if (!activeIds.has(id)) delete next[id];
      return next;
    });
  }, [tasks]);

  return history;
}
