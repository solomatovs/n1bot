import type { ReactElement, ReactNode } from "react";

import "./Code.css";

type Props = {
  /** Внутри панели на surface: фон страницы, чтобы блок читался. */
  inset?: boolean;
  tone?: "default" | "error";
  mark?: string | undefined;
  children: ReactNode;
};

/** Код в рамке с прокруткой: определение view, тело рутины, аргументы и
 * вывод задачи. Единственное место, где существует класс `code`. */
export function Code({ inset = false, tone = "default", mark, children }: Props): ReactElement {
  const classes = ["code"];
  if (inset) {
    classes.push("code--inset");
  }
  if (tone === "error") {
    classes.push("code--error");
  }

  return (
    <pre className={classes.join(" ")} data-testid={mark}>
      {children}
    </pre>
  );
}
