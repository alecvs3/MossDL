import { pauseTask, restartTask, resumeTask, startTask, stopTask, type Task, type TaskAction } from "../api";

export async function performTaskAction(task: Task, action: TaskAction): Promise<Task> {
  switch (action) {
    case "pause":
      return pauseTask(task);
    case "resume":
      if (task.state === "canceled" || task.state === "failed") {
        return restartTask(task);
      }
      if (task.state === "queued") {
        return startTask(task);
      }
      return resumeTask(task);
    case "stop":
      return stopTask(task);
    case "restart":
      return restartTask(task);
  }
}
