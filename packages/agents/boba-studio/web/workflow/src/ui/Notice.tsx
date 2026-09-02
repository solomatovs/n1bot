import type { ReactElement, ReactNode } from "react";

import "./Notice.css";

type Props = {
  tone?: "info" | "error";
  children: ReactNode;
};

/** Строка-уведомление сцены: итог действия либо его отказ.
 * Единственное место, где существует класс `notice`. */
export function Notice({ tone = "info", children }: Props): ReactElement {
  const classes = ["notice"];
  if (tone === "error") {
    classes.push("notice--error");
  }

  return (
    <span className={classes.join(" ")} data-notice>
      {children}
    </span>
  );
}
