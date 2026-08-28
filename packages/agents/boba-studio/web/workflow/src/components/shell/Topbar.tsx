import { PanelLeft, Settings, Workflow } from "lucide-react";
import type { ReactElement } from "react";
import { NavLink } from "react-router-dom";

import type { StoredRun, StoredWorkflow } from "../../model/workflow";
import { ThemeToggle } from "../ThemeToggle";
import { SocketLamp } from "./SocketLamp";
import type { Mode } from "./Shell";

type Props = {
  mode: Mode;
  run: StoredRun | null;
  workflow: StoredWorkflow | null;
  listOpen: boolean;
  onToggleList: () => void;
};

export function shortRunId(runId: string): string {
  return runId.slice(0, 8);
}

/** Топбар: бренд, хлебные крошки текущего выбора, переключатель Observe/Build. */
export function Topbar({ mode, run, workflow, listOpen, onToggleList }: Props): ReactElement {
  const crumb = mode === "observe" ? "History" : "Workflows";
  let current = "";
  if (mode === "observe" && run !== null) {
    current = `${run.state.graph.spec.name} · ${shortRunId(run.id)}`;
  }
  if (mode === "build" && workflow !== null) {
    current = workflow.name;
  }

  return (
    <header className="topbar">
      <button
        type="button"
        className="icon-btn topbar__drawer"
        aria-label="Toggle list"
        aria-expanded={listOpen}
        onClick={onToggleList}
      >
        <PanelLeft size={16} />
      </button>
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
      <div className="segmented" role="tablist" aria-label="mode">
        <NavLink
          to="/observe"
          className={({ isActive }) => `segmented__item${isActive ? " segmented__item--on" : ""}`}
        >
          Observe
        </NavLink>
        <NavLink
          to="/build"
          className={({ isActive }) => `segmented__item${isActive ? " segmented__item--on" : ""}`}
        >
          Build
        </NavLink>
      </div>
      <SocketLamp />
      <ThemeToggle />
      <NavLink to="/account" className="icon-btn" aria-label="Account" title="Account">
        <Settings size={16} />
      </NavLink>
    </header>
  );
}
