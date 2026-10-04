import { X } from "lucide-react";
import type { ReactElement } from "react";

import { formatDuration, formatInstant } from "../../model/time";
import type { RunState, TaskSpec, TaskState } from "../../model/workflow";
import { JsonView } from "../JsonView";
import { ResultView } from "../results/ResultView";
import { OutputPanel } from "./OutputPanel";
import { Chip, Code, Facts, IconButton, Note, Panel, PanelHead, Section, type Fact } from "../../ui";

type Props = {
  runId: string;
  run: RunState;
  task: string;
  onClose: () => void;
};

/** Инспектор задачи в панели деталей запуска: факты, вывод, аргументы, итог. */
export function Inspector({ runId, run, task, onClose }: Props): ReactElement {
  const spec = run.graph.spec.tasks[task];
  const state = run.tasks[task];

  let status: ReactElement | null = null;
  if (state !== undefined) {
    status = <Chip status={state.status} />;
  }

  let body: ReactElement = <Note tone="muted">unknown task {task}</Note>;
  if (spec !== undefined && state !== undefined) {
    body = <TaskDetails runId={runId} spec={spec} state={state} />;
  }

  return (
    <Panel>
      <PanelHead
        eyebrow="task"
        name={task}
        mono
        actions={
          <>
            {status}
            <IconButton onClick={onClose} aria-label="Close inspector">
              <X size={14} />
            </IconButton>
          </>
        }
      />
      {body}
    </Panel>
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
    <>
      <Section>
        <Facts facts={facts} mark="task-facts" />
      </Section>
      {state.call_id !== "" && (
        <Section>
          <OutputPanel runId={runId} callId={state.call_id} />
        </Section>
      )}
      <Section title="args">
        <Code inset mark="task-args">
          <JsonView value={spec.args} clip={0} />
        </Code>
      </Section>
      {state.result !== null && (
        <Section title="result">
          <ResultView state={state} />
        </Section>
      )}
      {state.error !== "" && (state.result === null || state.result.ok) && (
        <Section title="error">
          <Code inset tone="error">
            {state.error}
          </Code>
        </Section>
      )}
    </>
  );
}
