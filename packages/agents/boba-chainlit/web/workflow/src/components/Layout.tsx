import type { ReactElement } from "react";
import type { PropsWithChildren } from "react";
import { NavLink } from "react-router-dom";

import { ThemeToggle } from "./ThemeToggle";

export function Layout({ children }: PropsWithChildren): ReactElement {
  return (
    <div className="layout">
      <header className="header">
        <span className="header__brand">Boba · Workflow</span>
        <nav className="header__nav">
          <NavLink to="/" end>
            Workflows
          </NavLink>
          <NavLink to="/new">New</NavLink>
        </nav>
        <span className="header__spacer" />
        <ThemeToggle />
      </header>
      {children}
    </div>
  );
}
