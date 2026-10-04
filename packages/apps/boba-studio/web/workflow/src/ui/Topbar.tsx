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

/** Марка приложения в шапке: иконка и имя. */
export function TopbarBrand({ children }: { children: ReactNode }): ReactElement {
  return <div className="topbar__brand">{children}</div>;
}

/** Слот кнопки панели: держит сетку шапки той же на страницах без панели. */
export function TopbarSlot(): ReactElement {
  return <span className="topbar__slot" aria-hidden="true" />;
}

type CrumbsProps = {
  /** Корень раздела и текущая запись в нём. */
  root: ReactNode;
  current?: string | undefined;
};

/** Крошки шапки: раздел и текущая запись; на узком экране прячутся. */
export function TopbarCrumbs({ root, current }: CrumbsProps): ReactElement {
  return (
    <nav className="topbar__crumbs" aria-label="breadcrumbs">
      <span>{root}</span>
      {current !== undefined && current !== "" && (
        <>
          <span className="topbar__crumbs-sep">›</span>
          <span className="topbar__crumbs-current">{current}</span>
        </>
      )}
    </nav>
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
