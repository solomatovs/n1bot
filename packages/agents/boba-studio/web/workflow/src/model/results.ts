import type { StudioSummary, TaskState } from "./workflow";

/** Сводка итога задачи для узла и рёбер: приходит готовой в task.view. */

export type ResultSummary = {
  kind: string;
  figure: string;
  detail: string;
};

export function resultSummary(state: TaskState): ResultSummary | null {
  if (state.result === null || state.view === null) {
    return null;
  }

  return { kind: state.result.kind, ...state.view.summary };
}

/** Подпись ребра-значения: что течёт из результата, `table ×12`. */
export function resultFlowLabel(state: TaskState): string {
  const summary = resultSummary(state);
  if (summary === null) {
    return "";
  }

  return flowOf(summary);
}

function flowOf(summary: StudioSummary & { kind: string }): string {
  if (summary.detail === "") {
    return summary.kind;
  }

  return `${summary.kind} ×${summary.figure}`;
}
