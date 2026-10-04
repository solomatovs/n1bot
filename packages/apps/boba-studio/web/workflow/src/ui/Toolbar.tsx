import type { ReactElement, ReactNode } from "react";

import "./Toolbar.css";

type Props = {
  /** Полоса над сценой: без переносов, с рамкой снизу. */
  bar?: boolean;
  mark?: string | undefined;
  children: ReactNode;
};

/** Ряд действий: кнопки формы, панель холста, подвал панели, полосы сцены
 * билдера и запуска.
 * Единственное место, где существует класс `toolbar`. */
export function Toolbar({ bar = false, mark, children }: Props): ReactElement {
  const classes = ["toolbar"];
  if (bar) {
    classes.push("toolbar--bar");
  }

  return (
    <div className={classes.join(" ")} data-testid={mark}>
      {children}
    </div>
  );
}

export function ToolbarSpacer(): ReactElement {
  return <span className="toolbar__spacer" />;
}

/** Заголовок полосы: имя сцены (билдер). */
export function ToolbarLabel({ children }: { children: ReactNode }): ReactElement {
  return <span className="toolbar__label">{children}</span>;
}

/** Подсказка полосы: инстанс запуска, правила рёбер. */
export function ToolbarHint({ children }: { children: ReactNode }): ReactElement {
  return <span className="toolbar__hint">{children}</span>;
}
