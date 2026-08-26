import { History, LayoutGrid } from "lucide-react";
import type { ReactElement } from "react";
import { NavLink } from "react-router-dom";

import type { Mode } from "./Shell";

type Props = {
  mode: Mode;
};

/** Рейл: History — запуски, Workflows — определения. */
export function Rail({ mode }: Props): ReactElement {
  return (
    <nav className="rail" aria-label="sections">
      <NavLink
        to="/observe"
        className={`rail__item${mode === "observe" ? " rail__item--on" : ""}`}
        title="History"
      >
        <History size={20} />
        History
      </NavLink>
      <NavLink
        to="/build"
        className={`rail__item${mode === "build" ? " rail__item--on" : ""}`}
        title="Workflows"
      >
        <LayoutGrid size={20} />
        Workflows
      </NavLink>
    </nav>
  );
}
