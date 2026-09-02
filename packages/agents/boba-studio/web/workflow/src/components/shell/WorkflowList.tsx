import { type ReactElement, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { runsOfWorkflow } from "../../hooks/useShellData";
import { parseSpecText } from "../../model/spec";
import { formatAgo, formatDuration } from "../../model/time";
import type { Initiator, StoredRun, StoredWorkflow, WorkflowDraft } from "../../model/workflow";
import { Chip, EmptyState, Eyebrow, ListRow } from "../../ui";

type Props = {
  workflows: StoredWorkflow[];
  drafts: WorkflowDraft[];
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

/** Имя черновика — из его спеки; битый YAML не роняет список. */
export function draftName(draft: WorkflowDraft): string {
  try {
    const name = parseSpecText(draft.spec).name.trim();
    if (name !== "") {
      return name;
    }
  } catch {
    // черновик правится по ходу: спека бывает недописанной
  }

  return "unnamed";
}

/** Адрес черновика: новый открывается своим uuid, черновик сохранённого — самим workflow. */
export function draftHref(draft: WorkflowDraft): string {
  const [kind, ident] = draft.key.split(":", 2);
  if (kind === "new") {
    return `/workflow/new?draft=${ident}`;
  }

  return `/workflow/${ident}`;
}

function taskCount(run: StoredRun): number {
  return Object.keys(run.state.tasks).length;
}

function failedCount(run: StoredRun): number {
  return Object.values(run.state.tasks).filter((task) => task.status === "failed").length;
}

/** Список workflow — и выбор, и история: черновики с пометкой сверху, у каждого
 * workflow стрелка разворачивает его запуски, свежие сверху. */
export function WorkflowList({
  workflows,
  drafts,
  runs,
  selectedWorkflow,
  selectedRun,
  onPick,
}: Props): ReactElement {
  const [filter, setFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (needle === "") {
      return workflows;
    }

    return workflows.filter((item) => item.name.toLowerCase().includes(needle));
  }, [workflows, filter]);

  // отдельной строкой живут только черновики НОВЫХ workflow: черновик
  // сохранённого — это его правки, они открываются самим workflow
  const shownDrafts = useMemo(() => {
    const fresh = drafts.filter((draft) => draft.key.startsWith("new:"));
    const needle = filter.trim().toLowerCase();
    if (needle === "") {
      return fresh;
    }

    return fresh.filter((draft) => draftName(draft).toLowerCase().includes(needle));
  }, [drafts, filter]);

  const editedWorkflows = useMemo(() => {
    const edited = new Set<string>();
    for (const draft of drafts) {
      const [kind, ident] = draft.key.split(":", 2);
      if (kind === "workflow" && ident !== undefined) {
        edited.add(ident);
      }
    }

    return edited;
  }, [drafts]);

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
        <Link to="/workflow/new" className="list__new" onClick={onPick}>
          + New workflow
        </Link>
        {shownDrafts.map((draft) => (
          <ListRow
            key={draft.key}
            href={draftHref(draft)}
            draft
            dotColor="var(--muted)"
            dataDraft={draft.key}
            name={draftName(draft)}
            pills={<Chip tone="draft">draft</Chip>}
            onClick={onPick}
          />
        ))}
        {shown.length === 0 && shownDrafts.length === 0 && <EmptyState>No workflows in this filter.</EmptyState>}
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
                    {editedWorkflows.has(item.id) && <Chip tone="draft">draft</Chip>}
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
