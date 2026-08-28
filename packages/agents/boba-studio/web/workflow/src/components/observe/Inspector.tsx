import { X } from "lucide-react";
import type { ReactElement } from "react";

import { formatDuration, formatInstant } from "../../model/time";
import type { RunState } from "../../model/workflow";
import { JsonView } from "../JsonView";
import { ResultView } from "../results/ResultView";
import { StatusPill } from "../StatusPill";
import { OutputPanel } from "./OutputPanel";

type Props = {
  runId: string;
  run: RunState;
  task: string;
  onClose: () => void;
};

/** Инспектор задачи: выезжает справа, как в Studio. */
export function Inspector({ runId, run, task, onClose }: Props): ReactElement {
  const spec = run.graph.spec.tasks[task];
  const state = run.tasks[task];

  return (
    <aside className="inspector" aria-label="inspector">
      <div className="inspector__head">
        <span className="eyebrow">task</span>
        <h3 className="inspector__title">{task}</h3>
        {state !== undefined && <StatusPill status={state.status} />}
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close inspector">
          <X size={14} />
        </button>
      </div>
      {spec === undefined || state === undefined ? (
        <div className="inspector__body muted">unknown task {task}</div>
      ) : (
        <div className="inspector__body">
          <dl className="inspector__facts">
            <dt>tool</dt>
            <dd>{spec.tool}</dd>
            <dt>started</dt>
            <dd>{formatInstant(state.started_at)}</dd>
            <dt>duration</dt>
            <dd>{formatDuration(state.started_at, state.finished_at)}</dd>
            {state.call_id !== "" && (
              <>
                <dt>call</dt>
                <dd>{state.call_id}</dd>
              </>
            )}
          </dl>
          {state.call_id !== "" && (
            <OutputPanel runId={runId} callId={state.call_id} live={state.status === "running"} />
          )}
          <h4 className="eyebrow">args</h4>
          <div className="inspector__code">
            <JsonView value={spec.args} clip={0} />
          </div>
          {state.result !== null && (
            <>
              <h4 className="eyebrow">result</h4>
              <ResultView result={state.result} />
            </>
          )}
          {state.error !== "" && (state.result === null || state.result.ok) && (
            <>
              <h4 className="eyebrow">error</h4>
              <pre className="inspector__code inspector__code--error">{state.error}</pre>
            </>
          )}
        </div>
      )}
    </aside>
  );
}
