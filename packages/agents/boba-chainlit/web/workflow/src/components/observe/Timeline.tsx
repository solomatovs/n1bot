import { type ReactElement, useState } from "react";

import { phaseColor } from "../../model/summary";
import { formatClock, parseInstant } from "../../model/time";
import type { RunState } from "../../model/workflow";

type Props = {
  run: RunState;
  startedAt: string;
  now: number;
  onSelect: (task: string) => void;
};

type Bar = {
  task: string;
  status: string;
  start: number;
  end: number;
};

const TICKS = 5;

/** Таймлайн Studio: ось времени от старта, группы по стадиям, полосы цвета фазы, курсор. */
export function Timeline({ run, startedAt, now, onSelect }: Props): ReactElement {
  const [cursor, setCursor] = useState<number | null>(null);
  const origin = parseInstant(startedAt) ?? now;

  let end = origin + 1000;
  const bars = new Map<string, Bar>();
  for (const [task, state] of Object.entries(run.tasks)) {
    const start = parseInstant(state.started_at);
    if (start === null) {
      continue;
    }

    const finish = parseInstant(state.finished_at) ?? now;
    end = Math.max(end, finish);
    bars.set(task, { task, status: state.status, start: start - origin, end: finish - origin });
  }

  const span = Math.max(end - origin, 1000);
  const percent = (millis: number): number => (millis / span) * 100;
  const cursorMs = cursor === null ? null : cursor * span;
  const active = cursorMs === null ? 0 : [...bars.values()].filter((bar) => bar.start <= cursorMs && cursorMs <= bar.end).length;

  return (
    <div className="view view--scroll">
      <div className="tl">
        <div className="tl__axis">
          <span className="viewbar__hint">times from the run</span>
          <div className="tl__ticks">
            {Array.from({ length: TICKS }, (_unused, index) => {
              const at = (span * index) / (TICKS - 1);
              return (
                <span className="tl__tick" key={index} style={{ left: `${percent(at)}%` }}>
                  {formatClock(at)}
                </span>
              );
            })}
          </div>
        </div>
        <div
          className="tl__body"
          onMouseMove={(event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            const labelWidth = parseFloat(getComputedStyle(event.currentTarget).getPropertyValue("--tl-label")) || 220;
            const x = event.clientX - rect.left - labelWidth;
            const width = rect.width - labelWidth;
            setCursor(x < 0 || width <= 0 ? null : Math.min(1, x / width));
          }}
          onMouseLeave={() => {
            setCursor(null);
          }}
        >
          {run.graph.stages.map((stage, index) => (
            <div key={stage.id}>
              <div className="tl__group" style={{ "--phase-color": phaseColor(index) } as React.CSSProperties}>
                {stage.id.replace(/^stage:/, "")}
              </div>
              {stage.tasks.map((task) => {
                const bar = bars.get(task);
                const status = run.tasks[task]?.status ?? "pending";
                return (
                  <div className="tl__row" key={task} data-status={status}>
                    <span className="tl__label">
                      <span className={`tl__mark${status === "failed" ? " tl__mark--failed" : ""}`}>
                        {mark(status)}
                      </span>
                      {task}
                    </span>
                    <div className="tl__lane">
                      {bar !== undefined && (
                        <button
                          type="button"
                          className="tl__bar"
                          data-status={bar.status}
                          style={
                            {
                              left: `${percent(bar.start)}%`,
                              width: `${Math.max(percent(bar.end - bar.start), 0.3)}%`,
                              "--bar": phaseColor(index),
                            } as React.CSSProperties
                          }
                          title={`${task}: ${formatClock(bar.start)} → ${formatClock(bar.end)}`}
                          onClick={() => {
                            onSelect(task);
                          }}
                        />
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ))}
          {cursor !== null && (
            <div className="tl__cursor" style={{ left: `calc(var(--tl-label) + ${cursor * 100}% - var(--tl-label) * ${cursor})` }} />
          )}
        </div>
        <div className="tl__foot">
          cursor <b>{formatClock(cursorMs ?? 0)}</b> · <b>{active}</b> active
        </div>
      </div>
    </div>
  );
}

function mark(status: string): string {
  switch (status) {
    case "done":
      return "✓";
    case "failed":
      return "✕";
    case "running":
      return "●";
    case "skipped":
      return "–";
    case "stopped":
      return "■";
    default:
      return "○";
  }
}
