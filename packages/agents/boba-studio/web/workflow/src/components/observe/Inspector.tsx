import { X } from "lucide-react";
import type { ReactElement } from "react";

import { formatDuration, formatInstant } from "../../model/time";
import type { RunState, TaskSpec, TaskState } from "../../model/workflow";
import { JsonView } from "../JsonView";
import { ResultView } from "../results/ResultView";
import { OutputPanel } from "./OutputPanel";
import { Chip, Code, Eyebrow, Facts, IconButton, type Fact } from "../../ui";

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
        <Eyebrow>task</Eyebrow>
        <h3 className="inspector__title">{task}</h3>
        {state !== undefined && <Chip status={state.status} />}
        <IconButton onClick={onClose} aria-label="Close inspector">
          <X size={14} />
        </IconButton>
      </div>
      {spec === undefined || state === undefined ? (
        <div className="inspector__body muted">unknown task {task}</div>
      ) : (
        <TaskDetails runId={runId} spec={spec} state={state} />
      )}
    </aside>
  );
}

type DetailsProps = {
  runId: string;
  spec: TaskSpec;
  state: TaskState;
};

function TaskDetails({ runId, spec, state }: DetailsProps): ReactElement {
  const facts: Fact[] = [
    { key: "tool", label: "tool", value: spec.tool },
    { key: "started", label: "started", value: formatInstant(state.started_at) },
    { key: "duration", label: "duration", value: formatDuration(state.started_at, state.finished_at) },
  ];
  if (state.call_id !== "") {
    facts.push({ key: "call", label: "call", value: state.call_id });
  }

  return (
    <div className="inspector__body">
      <Facts facts={facts} mark="task-facts" />
      {state.call_id !== "" && <OutputPanel runId={runId} callId={state.call_id} />}
      <Eyebrow as="h4">args</Eyebrow>
      <Code inset mark="task-args">
        <JsonView value={spec.args} clip={0} />
      </Code>
      {state.result !== null && (
        <>
          <Eyebrow as="h4">result</Eyebrow>
          <ResultView result={state.result} />
        </>
      )}
      {state.error !== "" && (state.result === null || state.result.ok) && (
        <>
          <Eyebrow as="h4">error</Eyebrow>
          <Code inset tone="error">
            {state.error}
          </Code>
        </>
      )}
    </div>
  );
}
