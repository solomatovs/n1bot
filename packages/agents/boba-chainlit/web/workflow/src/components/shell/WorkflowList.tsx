import { type ReactElement, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { runsOfWorkflow } from "../../hooks/useShellData";
import type { StoredRun, StoredWorkflow } from "../../model/workflow";

type Props = {
  workflows: StoredWorkflow[];
  runs: StoredRun[];
  selected: string | null;
  open: boolean;
};

/** Список workflow: фильтр, «+ New workflow», инструменты пилюлями, число запусков. */
export function WorkflowList({ workflows, runs, selected, open }: Props): ReactElement {
  const [filter, setFilter] = useState("");
  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (needle === "") {
      return workflows;
    }

    return workflows.filter((item) => item.name.toLowerCase().includes(needle));
  }, [workflows, filter]);

  return (
    <aside className={`list${open ? " list--open" : ""}`} aria-label="workflows">
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
        <Link to="/build/new" className="list__new">
          + New workflow
        </Link>
        {shown.length === 0 && <div className="empty">No workflows in this filter.</div>}
        {shown.map((item) => {
          const count = runsOfWorkflow(runs, item.id).length;
          return (
            <Link
              key={item.id}
              to={`/build/${item.id}`}
              className={`item${String(item.id) === selected ? " item--on" : ""}`}
            >
              <span className="item__dot" style={{ "--status-color": "var(--signal)" } as React.CSSProperties} />
              <span className="item__name">{item.name}</span>
              <span className="item__pills">
                {item.tools.map((tool) => (
                  <span className="chip" key={tool}>
                    {tool}
                  </span>
                ))}
                <span className="chip chip--muted">{count} runs</span>
              </span>
            </Link>
          );
        })}
      </div>
    </aside>
  );
}
