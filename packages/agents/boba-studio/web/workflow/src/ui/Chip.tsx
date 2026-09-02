import type { ReactElement, ReactNode } from "react";

import "./Chip.css";

export type ChipTone = "default" | "muted" | "draft";

type Props = {
  tone?: ChipTone;
  children: ReactNode;
};

/** Пилюля-метка: имя инструмента, счётчик, пометка draft.
 * Единственное место, где существует класс `chip`. */
export function Chip({ tone = "default", children }: Props): ReactElement {
  const classes = ["chip"];
  if (tone !== "default") {
    classes.push(`chip--${tone}`);
  }

  return <span className={classes.join(" ")}>{children}</span>;
}
