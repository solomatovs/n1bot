import type { ReactElement, ReactNode } from "react";

import "./Toolbar.css";

type Props = {
  /** builder — панель действий билдера, view — строка сцены запуска. */
  variant: "builder" | "view";
  children: ReactNode;
};

/** Горизонтальная панель действий сцены.
 * Единственное место с классами `builder` и `viewbar`. */
export function Toolbar({ variant, children }: Props): ReactElement {
  return <div className={variant === "builder" ? "builder" : "viewbar"}>{children}</div>;
}

export function ToolbarLabel({ children }: { children: ReactNode }): ReactElement {
  return <span className="builder__label">{children}</span>;
}

export function ToolbarHint({
  children,
  variant = "view",
}: {
  children: ReactNode;
  variant?: "builder" | "view";
}): ReactElement {
  return <span className={variant === "builder" ? "builder__hint" : "viewbar__hint"}>{children}</span>;
}

/** Распорка: всё после неё прижимается к правому краю. */
export function ToolbarSpacer(): ReactElement {
  return <span className="viewbar__spacer" />;
}
