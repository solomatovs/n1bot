import { ChevronDown, ChevronRight } from "lucide-react";
import { type ReactElement, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { formatAgo, formatDuration } from "../../model/time";
import type { Initiator, StoredRun } from "../../model/workflow";

type Props = {
  runs: StoredRun[];
  selected: string | null;
  open: boolean;
};

type Group = {
  name: string;
  runs: StoredRun[];
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

function groupsOf(runs: StoredRun[]): Group[] {
  const byName = new Map<string, StoredRun[]>();
  for (const run of runs) {
    const name = run.state.graph.spec.name;
    const bucket = byName.get(name);
    if (bucket === undefined) {
      byName.set(name, [run]);
    } else {
      bucket.push(run);
    }
  }

  return [...byName.entries()].map(([name, grouped]) => ({ name, runs: grouped }));
}

function taskCount(run: StoredRun): number {
  return Object.keys(run.state.tasks).length;
}

function failedCount(run: StoredRun): number {
  return Object.values(run.state.tasks).filter((task) => task.status === "failed").length;
}

/** Список запусков: фильтр, группы по workflow, статус точкой, выбранный — подсвечен. */
export function RunList({ runs, selected, open }: Props): ReactElement {
  const [filter, setFilter] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());

  const groups = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const shown = needle === "" ? runs : runs.filter((run) => run.state.graph.spec.name.toLowerCase().includes(needle));
    return groupsOf(shown);
  }, [runs, filter]);

  const toggle = (name: string): void => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }

      return next;
    });
  };

  return (
    <aside className={`list${open ? " list--open" : ""}`} aria-label="runs">
      <div className="list__head">
        <span className="eyebrow">Runs</span>
        <span className="list__count">{runs.length}</span>
      </div>
      <input
        className="list__filter"
        placeholder="filter runs…"
        value={filter}
        onChange={(event) => {
          setFilter(event.target.value);
        }}
        aria-label="filter runs"
      />
      <div className="list__scroll">
        {groups.length === 0 && <div className="empty">No runs in this filter.</div>}
        {groups.map((group) => (
          <div key={group.name}>
            <button type="button" className="list__group" onClick={() => { toggle(group.name); }}>
              {collapsed.has(group.name) ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
              <span>{group.name}</span>
              <span className="list__group-count">{group.runs.length}</span>
            </button>
            {!collapsed.has(group.name) &&
              group.runs.map((run) => <RunItem key={run.id} run={run} selected={run.id === selected} />)}
          </div>
        ))}
      </div>
    </aside>
  );
}

function RunItem({ run, selected }: { run: StoredRun; selected: boolean }): ReactElement {
  const failed = failedCount(run);
  return (
    <Link to={`/observe/${run.id}`} className={`item${selected ? " item--on" : ""}`} data-status={run.status}>
      <span
        className="item__dot"
        data-status={run.status}
        style={{ "--status-color": `var(--status-${run.status})` } as React.CSSProperties}
      />
      <span className="item__name">{run.state.graph.spec.name}</span>
      <span className="item__meta">
        <span>{taskCount(run)} tasks</span>
        <span>{describeInitiator(run.initiator)}</span>
        <span>{formatAgo(run.started_at)}</span>
        <span>{formatDuration(run.started_at, run.finished_at)}</span>
        {failed > 0 && <span className="is-error">failed {failed}/{taskCount(run)}</span>}
      </span>
    </Link>
  );
}
