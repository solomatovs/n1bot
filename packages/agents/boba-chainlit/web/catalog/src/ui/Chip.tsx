import type { ReactElement, ReactNode } from "react";

import "./Chip.css";

export type ChipTone = "default" | "muted" | "draft" | "warn";

type Props = {
  tone?: ChipTone;
  /** Метка для тестов: data-testid. */
  mark?: string;
  title?: string;
  children: ReactNode;
};

/** Пилюля-метка: вид источника, версия, пометка draft, счётчик устаревших.
 * Единственное место, где существует класс `chip`. */
export function Chip({ tone = "default", mark, title, children }: Props): ReactElement {
  const classes = ["chip"];
  if (tone !== "default") {
    classes.push(`chip--${tone}`);
  }

  return (
    <span className={classes.join(" ")} data-testid={mark} title={title}>
      {children}
    </span>
  );
}
