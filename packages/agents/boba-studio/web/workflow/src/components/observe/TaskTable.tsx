import type { ReactElement } from "react";

import { formatDuration, formatInstant } from "../../model/time";
import type { RunState } from "../../model/workflow";
import { Cell, Chip, DataTable, SceneView, TableRow } from "../../ui";

type Props = {
  run: RunState;
  onSelect: (task: string) => void;
};

const HEAD = ["Stage", "Task", "Tool", "Status", "Started", "Duration", "Error"];

/** Табличный вид: задачи по стадиям со статусом, временем и ошибкой. */
export function TaskTable({ run, onSelect }: Props): ReactElement {
  const rows: ReactElement[] = [];
  for (const stage of run.graph.stages) {
    for (const task of stage.tasks) {
      const state = run.tasks[task];
      const spec = run.graph.spec.tasks[task];
      let status: ReactElement | string = "?";
      let duration = "—";
      if (state !== undefined) {
        status = <Chip status={state.status} />;
        duration = formatDuration(state.started_at, state.finished_at);
      }

      rows.push(
        <TableRow
          key={task}
          onClick={() => {
            onSelect(task);
          }}
        >
          <Cell className="faint">{stage.id}</Cell>
          <Cell>{task}</Cell>
          <Cell className="muted">{spec?.tool ?? "?"}</Cell>
          <Cell>{status}</Cell>
          <Cell className="muted">{formatInstant(state?.started_at ?? null)}</Cell>
          <Cell className="muted">{duration}</Cell>
          <Cell className="muted">{state?.error ?? ""}</Cell>
        </TableRow>,
      );
    }
  }

  return (
    <SceneView scroll>
      <DataTable head={HEAD} mark="task-table">
        {rows}
      </DataTable>
    </SceneView>
  );
}
