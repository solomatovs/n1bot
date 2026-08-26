import { parseInstant } from "./time";
import type { RunState, StoredRun, TaskState } from "./workflow";

/** Сводка запуска для полосы vitals: чистая логика над RunState. */

export type StatusCounts = Record<"done" | "running" | "pending" | "failed" | "skipped" | "stopped", number>;

export type RunSummary = {
  total: number;
  finished: number;
  counts: StatusCounts;
  stagesDone: number;
  stagesTotal: number;
  elapsedMs: number;
  longest: { task: string; ms: number } | null;
};

const EMPTY_COUNTS: StatusCounts = { done: 0, running: 0, pending: 0, failed: 0, skipped: 0, stopped: 0 };

function taskMs(state: TaskState, now: number): number | null {
  const start = parseInstant(state.started_at);
  if (start === null) {
    return null;
  }

  const finish = parseInstant(state.finished_at) ?? now;
  return Math.max(0, finish - start);
}

export function summarize(run: StoredRun, now: number): RunSummary {
  const state: RunState = run.state;
  const counts: StatusCounts = { ...EMPTY_COUNTS };
  let longest: RunSummary["longest"] = null;
  for (const [task, taskState] of Object.entries(state.tasks)) {
    counts[taskState.status] += 1;
    const ms = taskMs(taskState, now);
    if (ms !== null && (longest === null || ms > longest.ms)) {
      longest = { task, ms };
    }
  }

  const total = Object.keys(state.tasks).length;
  const finished = total - counts.pending - counts.running;
  const stagesDone = state.graph.stages.filter((stage) =>
    stage.tasks.every((task) => {
      const status = state.tasks[task]?.status ?? "pending";
      return status !== "pending" && status !== "running";
    }),
  ).length;

  const started = parseInstant(run.started_at) ?? now;
  const ended = parseInstant(run.finished_at) ?? now;
  return {
    total,
    finished,
    counts,
    stagesDone,
    stagesTotal: state.graph.stages.length,
    elapsedMs: Math.max(0, ended - started),
    longest,
  };
}

/** Индекс стадии → цвет фазы: шесть токенов по кругу. */
export function phaseColor(index: number): string {
  return `var(--phase-${index % 6})`;
}

export function stageIndex(run: RunState, task: string): number {
  return Math.max(
    0,
    run.graph.stages.findIndex((stage) => stage.tasks.includes(task)),
  );
}
