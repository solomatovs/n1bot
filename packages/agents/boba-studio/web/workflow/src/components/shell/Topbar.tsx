import { PanelLeft, Settings, Workflow } from "lucide-react";
import type { ReactElement } from "react";

import type { StoredRun, StoredWorkflow } from "../../model/workflow";
import { ThemeToggle } from "../ThemeToggle";
import { SocketLamp } from "./SocketLamp";
import { IconButton, IconLink } from "../../ui";

type Props = {
  run: StoredRun | null;
  workflow: StoredWorkflow | null;
  listOpen: boolean;
  onToggleList: () => void;
};

export function shortRunId(runId: string): string {
  return runId.slice(0, 8);
}

/** Топбар: бренд и хлебные крошки текущего выбора — workflow и его запуск. */
export function Topbar({ run, workflow, listOpen, onToggleList }: Props): ReactElement {
  const crumb = "Workflows";
  let current = "";
  if (workflow !== null) {
    current = workflow.name;
  }
  if (run !== null) {
    const name = workflow?.name ?? run.state.graph.spec.name;
    current = `${name} · ${shortRunId(run.id)}`;
  }

  return (
    <header className="topbar">
      <IconButton
        className="topbar__drawer"
        aria-label="Toggle list"
        aria-expanded={listOpen}
        onClick={onToggleList}
      >
        <PanelLeft size={16} />
      </IconButton>
      <div className="topbar__brand">
        <Workflow size={18} />
        <b>Boba</b> Workflow <span>Studio</span>
      </div>
      <nav className="crumbs" aria-label="breadcrumbs">
        <span>{crumb}</span>
        {current !== "" && (
          <>
            <span className="crumbs__sep">›</span>
            <span className="crumbs__current">{current}</span>
          </>
        )}
      </nav>
      <span className="topbar__spacer" />
      <SocketLamp />
      <ThemeToggle />
      <IconLink to="/account" aria-label="Account" title="Account">
        <Settings size={16} />
      </IconLink>
    </header>
  );
}
