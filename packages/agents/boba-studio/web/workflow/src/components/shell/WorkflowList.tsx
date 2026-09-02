import { ChevronDown, ChevronRight } from "lucide-react";
import { type CSSProperties, type ReactElement, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { runsOfWorkflow } from "../../hooks/useShellData";
import { parseSpecText } from "../../model/spec";
import { formatAgo, formatDuration } from "../../model/time";
import type { Initiator, StoredRun, StoredWorkflow, WorkflowDraft } from "../../model/workflow";

type Props = {
  workflows: StoredWorkflow[];
  drafts: WorkflowDraft[];
  runs: StoredRun[];
  selectedWorkflow: string | null;
  selectedRun: string | null;
  open: boolean;
  collapsed: boolean;
  onPick: () => void;
  onResizeStart: (event: React.PointerEvent<HTMLElement>) => void;
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
  open,
  collapsed,
  onPick,
  onResizeStart,
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

  const shownDrafts = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (needle === "") {
      return drafts;
    }

    return drafts.filter((draft) => draftName(draft).toLowerCase().includes(needle));
  }, [drafts, filter]);

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
    <aside
      className={`list${open ? " list--open" : ""}${collapsed ? " list--collapsed" : ""}`}
      aria-label="workflows"
    >
      <div className="list__head">
        <span className="eyebrow">Workflows</span>
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
          <Link
            to={draftHref(draft)}
            className="item item--draft"
            key={draft.key}
            data-draft={draft.key}
            onClick={onPick}
          >
            <span className="item__dot" style={{ "--status-color": "var(--muted)" } as CSSProperties} />
            <span className="item__name">{draftName(draft)}</span>
            <span className="item__pills">
              <span className="chip chip--draft">draft</span>
            </span>
          </Link>
        ))}
        {shown.length === 0 && shownDrafts.length === 0 && <div className="empty">No workflows in this filter.</div>}
        {shown.map((item) => {
          const own = runsOfWorkflow(runs, item.id);
          const opened = expanded.has(item.id);
          return (
            <div key={item.id}>
              <div className={`item${item.id === selectedWorkflow ? " item--on" : ""}`}>
                <button
                  type="button"
                  className="item__toggle"
                  aria-label={`runs of ${item.name}`}
                  aria-expanded={opened}
                  onClick={() => {
                    toggle(item.id);
                  }}
                >
                  {opened ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                </button>
                <Link to={`/workflow/${item.id}`} className="item__body" onClick={onPick}>
                  <span className="item__name">{item.name}</span>
                  <span className="item__pills">
                    {item.tools.map((tool) => (
                      <span className="chip" key={tool}>
                        {tool}
                      </span>
                    ))}
                    <span className="chip chip--muted">{own.length} runs</span>
                    {own[0] !== undefined && (
                      <span className="chip chip--muted">{formatAgo(own[0].started_at)}</span>
                    )}
                  </span>
                </Link>
              </div>
              {opened && own.length === 0 && <div className="empty empty--sub">No runs yet.</div>}
              {opened &&
                own.map((run) => (
                  <RunItem key={run.id} run={run} selected={run.id === selectedRun} onPick={onPick} />
                ))}
            </div>
          );
        })}
      </div>
      <div className="list__resize" aria-hidden="true" onPointerDown={onResizeStart} />
    </aside>
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
    <Link
      to={`/runs/${run.id}`}
      className={`item item--sub${selected ? " item--on" : ""}`}
      data-status={run.status}
      onClick={onPick}
    >
      <span
        className="item__dot"
        data-status={run.status}
        style={{ "--status-color": `var(--status-${run.status})` } as CSSProperties}
      />
      <span className="item__meta">
        <span>{taskCount(run)} tasks</span>
        <span>{describeInitiator(run.initiator)}</span>
        <span>{formatAgo(run.started_at)}</span>
        <span>{formatDuration(run.started_at, run.finished_at)}</span>
        {failed > 0 && (
          <span className="is-error">
            failed {failed}/{taskCount(run)}
          </span>
        )}
      </span>
    </Link>
  );
}
