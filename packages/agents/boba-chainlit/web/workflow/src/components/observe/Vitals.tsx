import type { ReactElement } from "react";

import { formatMs } from "../../model/time";
import { summarize } from "../../model/summary";
import type { StoredRun } from "../../model/workflow";

type Props = {
  run: StoredRun;
  now: number;
};

const DOTS = ["done", "running", "pending", "failed"] as const;

/** Полоса vitals: задачи, прогресс, точки статусов, стадии, время, самая долгая. */
export function Vitals({ run, now }: Props): ReactElement {
  const summary = summarize(run, now);
  const percent = summary.total === 0 ? 0 : Math.round((summary.finished / summary.total) * 100);

  return (
    <div className="vitals" aria-label="vitals">
      <span className="vitals__count">
        <b>{summary.total}</b> tasks
      </span>
      <div className="vitals__progress" role="progressbar" aria-valuenow={percent}>
        <div className="vitals__progress-fill" style={{ width: `${percent}%` }} />
      </div>
      <span className="vitals__dots">
        {DOTS.map((status) => (
          <span
            key={status}
            className={`vitals__dot${summary.counts[status] === 0 ? " vitals__dot--zero" : ""}`}
            data-status={status}
            style={{ "--status-color": `var(--status-${status})` } as React.CSSProperties}
            title={status}
          >
            <b>{summary.counts[status]}</b>
          </span>
        ))}
      </span>
      <Kpi label="stages" value={`${summary.stagesDone}/${summary.stagesTotal}`} />
      <Kpi label="time" value={formatMs(summary.elapsedMs)} />
      <Kpi label="longest" value={summary.longest === null ? "—" : formatMs(summary.longest.ms)} />
      <span
        className="vitals__badge"
        data-status={run.status}
        style={{ "--status-color": `var(--status-${run.status})` } as React.CSSProperties}
      >
        {run.status}
      </span>
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: string }): ReactElement {
  return (
    <span className="vitals__kpi">
      <span className="vitals__kpi-label">{label}</span>
      <span className="vitals__kpi-value">{value}</span>
    </span>
  );
}
