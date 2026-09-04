import type { ReactElement, ReactNode } from "react";

import "./Chip.css";

export type ChipTone = "default" | "muted" | "draft";

type Props = {
  tone?: ChipTone;
  /** Метка для тестов: data-testid. */
  mark?: string;
  children: ReactNode;
};

/** Пилюля-метка: имя инструмента, счётчик, пометка draft.
 * Единственное место, где существует класс `chip`. */
export function Chip({ tone = "default", mark, children }: Props): ReactElement {
  const classes = ["chip"];
  if (tone !== "default") {
    classes.push(`chip--${tone}`);
  }

  return (
    <span className={classes.join(" ")} data-testid={mark}>
      {children}
    </span>
  );
}
