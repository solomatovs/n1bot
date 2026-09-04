import type { ReactElement, ReactNode } from "react";

import "./EmptyState.css";

type Props = {
  title?: string;
  /** Заглушка сцены: растягивается на все строки грида родителя. */
  fill?: boolean;
  /** Заглушка под-списка: отступ под родительскую строку. */
  sub?: boolean;
  /** Метка для тестов: data-testid. */
  mark?: string;
  children?: ReactNode;
};

/** Пустое состояние: заглушка списка, под-списка или целой сцены.
 * Единственное место, где существует класс `empty`. */
export function EmptyState({ title, fill = false, sub = false, mark, children }: Props): ReactElement {
  const classes = ["empty"];
  if (sub) {
    classes.push("empty--sub");
  }
  if (fill) {
    classes.push("empty--fill");
  }

  return (
    <div className={classes.join(" ")} data-testid={mark}>
      {title !== undefined && <span className="empty__title">{title}</span>}
      {children !== undefined && <span>{children}</span>}
    </div>
  );
}
