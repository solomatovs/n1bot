import type { ReactElement, ReactNode } from "react";
import { Link } from "react-router-dom";

import "./Topbar.css";

/** Шапка страницы: ссылки-крошки, заголовок, чипы, действия, распорка,
 * подсказка. Единственное место, где существуют классы `topbar*`. */
export function Topbar({ children }: { children: ReactNode }): ReactElement {
  return <header className="topbar">{children}</header>;
}

export function TopbarLink({ to, children }: { to: string; children: ReactNode }): ReactElement {
  return (
    <Link to={to} className="topbar__link">
      {children}
    </Link>
  );
}

export function TopbarTitle({ children }: { children: ReactNode }): ReactElement {
  return (
    <span className="topbar__title" data-testid="page-title">
      {children}
    </span>
  );
}

export function TopbarSpacer(): ReactElement {
  return <span className="topbar__spacer" />;
}

export function TopbarHint({ mark, children }: { mark?: string; children: ReactNode }): ReactElement {
  return (
    <span className="topbar__hint" data-testid={mark}>
      {children}
    </span>
  );
}

/** Группа действий шапки: кнопки одного режима держатся вместе. */
export function TopbarGroup({ mark, children }: { mark?: string; children: ReactNode }): ReactElement {
  return (
    <span className="topbar__group" data-testid={mark}>
      {children}
    </span>
  );
}
