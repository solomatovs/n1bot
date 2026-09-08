import { z } from "zod";

export const RunStatusSchema = z.enum(["pending", "running", "done", "failed", "stopped"]);
export type RunStatus = z.infer<typeof RunStatusSchema>;

export const TaskStatusSchema = z.enum([
  "pending",
  "running",
  "done",
  "failed",
  "skipped",
  "stopped",
]);
export type TaskStatus = z.infer<typeof TaskStatusSchema>;

const TERMINAL_RUN: ReadonlySet<RunStatus> = new Set(["done", "failed", "stopped"]);

export function runFinished(status: RunStatus): boolean {
  return TERMINAL_RUN.has(status);
}
