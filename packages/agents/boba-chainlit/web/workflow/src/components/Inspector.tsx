import type { ReactElement } from "react";

import { StatusBadge } from "./StatusBadge";
import { formatDuration, formatInstant } from "../model/time";
import type { RunState } from "../model/workflow";

type Props = {
  run: RunState;
  task: string;
};

/** Правая панель: аргументы, статус и ошибка выбранной задачи. */
export function Inspector({ run, task }: Props): ReactElement {
  const spec = run.graph.spec.tasks[task];
  const state = run.tasks[task];
  if (spec === undefined || state === undefined) {
    return <aside className="inspector notice">unknown task {task}</aside>;
  }

  return (
    <aside className="inspector">
      <h3 className="inspector__title">
        {task} <StatusBadge status={state.status} />
      </h3>
      <dl className="inspector__facts">
        <dt>tool</dt>
        <dd className="mono">{spec.tool}</dd>
        <dt>started</dt>
        <dd>{formatInstant(state.started_at)}</dd>
        <dt>duration</dt>
        <dd>{formatDuration(state.started_at, state.finished_at)}</dd>
        {state.call_id !== "" && (
          <>
            <dt>call</dt>
            <dd className="mono">{state.call_id}</dd>
          </>
        )}
      </dl>
      <h4>args</h4>
      <pre className="inspector__code">{JSON.stringify(spec.args, null, 2)}</pre>
      {state.error !== "" && (
        <>
          <h4>error</h4>
          <pre className="inspector__code inspector__code--error">{state.error}</pre>
        </>
      )}
    </aside>
  );
}
