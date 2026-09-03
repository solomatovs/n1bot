import { type ReactElement, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useServices } from "../../app";
import { errorText } from "../Async";
import { runsOfWorkflow } from "../../hooks/useShellData";
import { renderSpecText } from "../../model/spec";
import { formatAgo, formatDuration } from "../../model/time";
import type { Initiator, StoredRun, StoredWorkflow } from "../../model/workflow";
import { Chip, EmptyState, Eyebrow, ListRow, useToast } from "../../ui";

type Props = {
  workflows: StoredWorkflow[];
  runs: StoredRun[];
  selectedWorkflow: string | null;
  selectedRun: string | null;
  onPick: () => void;
};

export function describeInitiator(initiator: Initiator): string {
  switch (initiator.kind) {
    case "chat":
      return "chat";
    case "llm":
      return "llm";
    case "human":
      return initiator.via;
    case "schedule":
      return `schedule ${initiator.job_id}`;
  }
}

/** Свободное имя нового workflow: new-workflow, new-workflow-2, … */
export function freeWorkflowName(taken: Iterable<string>): string {
  const used = new Set(taken);
  if (!used.has("new-workflow")) {
    return "new-workflow";
  }

  for (let index = 2; ; index += 1) {
    const candidate = `new-workflow-${index}`;
    if (!used.has(candidate)) {
      return candidate;
    }
  }
}

function taskCount(run: StoredRun): number {
  return Object.keys(run.state.tasks).length;
}

function failedCount(run: StoredRun): number {
  return Object.values(run.state.tasks).filter((task) => task.status === "failed").length;
}

/** Список workflow — и выбор, и история: New сразу создаёт строку, правки
 * помечаются chip'ом draft, у каждого workflow стрелка разворачивает запуски. */
export function WorkflowList({
  workflows,
  runs,
  selectedWorkflow,
  selectedRun,
  onPick,
}: Props): ReactElement {
  const { api } = useServices();
  const navigate = useNavigate();
  const toast = useToast();
  const [filter, setFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (needle === "") {
      return workflows;
    }

    return workflows.filter((item) => item.name.toLowerCase().includes(needle));
  }, [workflows, filter]);

  // выбранный запуск держит свой workflow развёрнутым
  const runWorkflow = useMemo(() => {
    if (selectedRun === null) {
      return null;
    }

    return runs.find((run) => run.id === selectedRun)?.workflow_id ?? null;
  }, [runs, selectedRun]);
  useEffect(() => {
    if (runWorkflow === null) {
      return;
    }

    setExpanded((current) => {
      if (current.has(runWorkflow)) {
        return current;
      }

      const next = new Set(current);
      next.add(runWorkflow);
      return next;
    });
  }, [runWorkflow]);

  const toggle = (id: string): void => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }

      return next;
    });
  };

  // New сразу пишет строку в хранилище: workflow существует до первой правки
  const create = async (): Promise<void> => {
    const name = freeWorkflowName(workflows.map((item) => item.name));
    const spec = renderSpecText({ name, description: "", tasks: [], edges: [] });

    try {
      const saved = await api.save(spec, { positions: {} });
      onPick();
      await navigate(`/workflow/${saved.id}`);
    } catch (error: unknown) {
      toast(`workflow not created: ${errorText(error)}`, "error");
    }
  };

  return (
    <>
      <div className="list__head">
        <Eyebrow>Workflows</Eyebrow>
        <span className="list__count">{workflows.length}</span>
      </div>
      <input
        className="list__filter"
        placeholder="filter workflows…"
        value={filter}
        onChange={(event) => {
          setFilter(event.target.value);
        }}
        aria-label="filter workflows"
      />
      <div className="list__scroll">
        <button type="button" className="list__new" onClick={() => void create()}>
          + New workflow
        </button>
        {shown.length === 0 && <EmptyState>No workflows in this filter.</EmptyState>}
        {shown.map((item) => {
          const own = runsOfWorkflow(runs, item.id);
          const opened = expanded.has(item.id);
          return (
            <div key={item.id}>
              <ListRow
                href={`/workflow/${item.id}`}
                selected={item.id === selectedWorkflow}
                toggle={{
                  expanded: opened,
                  label: `runs of ${item.name}`,
                  onToggle: () => {
                    toggle(item.id);
                  },
                }}
                name={item.name}
                pills={
                  <>
                    {item.draft_spec !== null && <Chip tone="draft">draft</Chip>}
                    {item.tools.map((tool) => (
                      <Chip key={tool}>{tool}</Chip>
                    ))}
                    <Chip tone="muted">{own.length} runs</Chip>
                    {own[0] !== undefined && <Chip tone="muted">{formatAgo(own[0].started_at)}</Chip>}
                  </>
                }
                onClick={onPick}
              />
              {opened && own.length === 0 && <EmptyState sub>No runs yet.</EmptyState>}
              {opened &&
                own.map((run) => (
                  <RunItem key={run.id} run={run} selected={run.id === selectedRun} onPick={onPick} />
                ))}
            </div>
          );
        })}
      </div>
    </>
  );
}

function RunItem({
  run,
  selected,
  onPick,
}: {
  run: StoredRun;
  selected: boolean;
  onPick: () => void;
}): ReactElement {
  const failed = failedCount(run);
  return (
    <ListRow
      href={`/runs/${run.id}`}
      sub
      selected={selected}
      status={run.status}
      onClick={onPick}
      meta={
        <>
          <span>{taskCount(run)} tasks</span>
          <span>{describeInitiator(run.initiator)}</span>
          <span>{formatAgo(run.started_at)}</span>
          <span>{formatDuration(run.started_at, run.finished_at)}</span>
          {failed > 0 && (
            <span className="is-error">
              failed {failed}/{taskCount(run)}
            </span>
          )}
        </>
      }
    />
  );
}
