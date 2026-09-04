import type { ReactElement, ReactNode } from "react";

import "./Note.css";

export type NoteTone = "muted" | "faint" | "error";

type Props = {
  tone?: NoteTone;
  micro?: boolean;
  mono?: boolean;
  /** Отступы строки панели: заметка вместо списка. */
  pad?: boolean;
  mark?: string | undefined;
  children: ReactNode;
};

/** Приглушённый текст: подсказка формы, пустой список, ошибка ветки.
 * Единственное место, где существует класс `note`. */
export function Note({
  tone = "muted",
  micro = false,
  mono = false,
  pad = false,
  mark,
  children,
}: Props): ReactElement {
  const classes = ["note"];
  if (tone !== "muted") {
    classes.push(`note--${tone}`);
  }
  if (micro) {
    classes.push("note--micro");
  }
  if (mono) {
    classes.push("mono");
  }
  if (pad) {
    classes.push("note--pad");
  }

  return (
    <p className={classes.join(" ")} data-testid={mark}>
      {children}
    </p>
  );
}
