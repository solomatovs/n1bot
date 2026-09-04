import type { ReactElement, ReactNode } from "react";

import "./Toolbar.css";

type Props = {
  /** Прижать действия вправо. */
  end?: boolean;
  /** Полоса над сценой: без переносов, с рамкой снизу. */
  bar?: boolean;
  /** Внутренние отступы: подвал панели. */
  pad?: boolean;
  mark?: string | undefined;
  children: ReactNode;
};

/** Ряд действий: кнопки формы, панель холста, подвал панели.
 * Единственное место, где существует класс `toolbar`. */
export function Toolbar({ end = false, bar = false, pad = false, mark, children }: Props): ReactElement {
  const classes = ["toolbar"];
  if (end) {
    classes.push("toolbar--end");
  }
  if (bar) {
    classes.push("toolbar--bar");
  }
  if (pad) {
    classes.push("toolbar--pad");
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
