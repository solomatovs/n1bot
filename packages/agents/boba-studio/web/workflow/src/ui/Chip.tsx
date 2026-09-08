import type { CSSProperties, ReactElement, ReactNode } from "react";

import type { RunStatus, TaskStatus } from "../model/status";
import "./Chip.css";

export type ChipTone = "default" | "muted" | "draft" | "warn" | "error";

type Props = {
  tone?: ChipTone;
  /** Статус запуска или задачи: пилюля с точкой цвета токена --status-<имя>;
   * текст — сам статус. */
  status?: RunStatus | TaskStatus | undefined;
  /** Метка для тестов: data-testid. */
  mark?: string;
  title?: string;
  children?: ReactNode;
};

/** Пилюля-метка: вид источника, версия, пометка draft, счётчик устаревших,
 * статус с точкой. Единственное место, где существует класс `chip`. */
export function Chip({ tone = "default", status, mark, title, children }: Props): ReactElement {
  const classes = ["chip"];
  if (tone !== "default") {
    classes.push(`chip--${tone}`);
  }

  if (status === undefined) {
    return (
      <span className={classes.join(" ")} data-testid={mark} title={title}>
        {children}
      </span>
    );
  }

  classes.push("chip--status");
  const style = { "--status-color": `var(--status-${status})` } as CSSProperties;

  return (
    <span className={classes.join(" ")} data-status={status} data-testid={mark} title={title} style={style}>
      {status}
    </span>
  );
}
