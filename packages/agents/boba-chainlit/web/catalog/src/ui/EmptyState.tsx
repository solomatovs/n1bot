import type { CSSProperties, ReactElement, ReactNode } from "react";

import "./EmptyState.css";

type Props = {
  title?: string;
  /** Заглушка сцены: растягивается на все строки грида родителя. */
  fill?: boolean;
  /** Заглушка под-списка: отступ под родительскую строку. */
  sub?: boolean;
  children?: ReactNode;
};

/** Пустое состояние: заглушка списка, под-списка или целой сцены.
 * Единственное место, где существует класс `empty`. */
export function EmptyState({ title, fill = false, sub = false, children }: Props): ReactElement {
  const classes = ["empty"];
  if (sub) {
    classes.push("empty--sub");
  }

  const style: CSSProperties | undefined = fill ? { gridRow: "1 / -1" } : undefined;
  return (
    <div className={classes.join(" ")} style={style}>
      {title !== undefined && <span className="empty__title">{title}</span>}
      {children !== undefined && <span>{children}</span>}
    </div>
  );
}
