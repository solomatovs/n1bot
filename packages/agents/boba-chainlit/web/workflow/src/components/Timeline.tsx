import type { ReactElement } from "react";

import { parseInstant } from "../model/time";
import type { RunState } from "../model/workflow";

type Props = {
  run: RunState;
  startedAt: string;
  now: number;
};

type Bar = {
  task: string;
  status: string;
  left: number;
  width: number;
};

/** Gantt по started/finished задач; идущие задачи тянутся до текущего момента. */
export function Timeline({ run, startedAt, now }: Props): ReactElement {
  const origin = parseInstant(startedAt) ?? now;
  const bars: Bar[] = [];
  let end = origin + 1;
  for (const [task, state] of Object.entries(run.tasks)) {
    const start = parseInstant(state.started_at);
    if (start === null) {
      continue;
    }

    const finish = parseInstant(state.finished_at) ?? now;
    end = Math.max(end, finish);
    bars.push({ task, status: state.status, left: start - origin, width: Math.max(finish - start, 1) });
  }

  const span = Math.max(end - origin, 1);
  return (
    <div className="timeline">
      {bars.length === 0 && <div className="faint">No task has started yet.</div>}
      {bars.map((bar) => (
        <div className="timeline__row" key={bar.task}>
          <div className="timeline__label mono">{bar.task}</div>
          <div className="timeline__track">
            <div
              className="timeline__bar"
              data-status={bar.status}
              style={{ left: `${(bar.left / span) * 100}%`, width: `${(bar.width / span) * 100}%` }}
              title={`${bar.task}: ${bar.width} ms`}
            />
          </div>
        </div>
      ))}
    </div>
  );
}
