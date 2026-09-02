import type { ReactElement } from "react";

import { formatDuration, formatInstant } from "../../model/time";
import type { RunState } from "../../model/workflow";
import { StatusPill } from "../../ui/StatusPill";

type Props = {
  run: RunState;
  onSelect: (task: string) => void;
};

/** Табличный вид: задачи по стадиям со статусом, временем и ошибкой. */
export function TaskTable({ run, onSelect }: Props): ReactElement {
  return (
    <div className="view view--scroll">
      <table className="table">
        <thead>
          <tr>
            <th>Stage</th>
            <th>Task</th>
            <th>Tool</th>
            <th>Status</th>
            <th>Started</th>
            <th>Duration</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          {run.graph.stages.flatMap((stage) =>
            stage.tasks.map((task) => {
              const state = run.tasks[task];
              const spec = run.graph.spec.tasks[task];
              return (
                <tr
                  key={task}
                  onClick={() => {
                    onSelect(task);
                  }}
                >
                  <td className="faint">{stage.id}</td>
                  <td>{task}</td>
                  <td className="muted">{spec?.tool ?? "?"}</td>
                  <td>{state === undefined ? "?" : <StatusPill status={state.status} />}</td>
                  <td className="muted">{formatInstant(state?.started_at ?? null)}</td>
                  <td className="muted">
                    {state === undefined ? "—" : formatDuration(state.started_at, state.finished_at)}
                  </td>
                  <td className="muted">{state?.error ?? ""}</td>
                </tr>
              );
            }),
          )}
        </tbody>
      </table>
    </div>
  );
}
