import type { ReactElement, ReactNode } from "react";

import "./EmptyState.css";

type Props = {
  title?: string;
  /** Заглушка сцены: растягивается на все строки грида родителя. */
  fill?: boolean;
  /** Заглушка под-списка: отступ под родительскую строку. */
  sub?: boolean;
  /** Подсказка поверх живой сцены: лежит над холстом и не перехватывает
   * мышь, чтобы на холст можно было бросать и тащить. */
  overlay?: boolean;
  /** Метка для тестов: data-testid. */
  mark?: string;
  children?: ReactNode;
};

/** Пустое состояние: заглушка списка, под-списка или целой сцены.
 * Единственное место, где существует класс `empty`. */
export function EmptyState({ title, fill = false, sub = false, overlay = false, mark, children }: Props): ReactElement {
  const classes = ["empty"];
  if (sub) {
    classes.push("empty--sub");
  }
  if (fill) {
    classes.push("empty--fill");
  }
  if (overlay) {
    classes.push("empty--overlay");
  }

  return (
    <div className={classes.join(" ")} data-testid={mark}>
      {title !== undefined && <span className="empty__title">{title}</span>}
      {children !== undefined && <div className="empty__body">{children}</div>}
    </div>
  );
}

type StepsProps = { children: ReactNode; mark?: string };

/** Нумерованный список шагов для пустого состояния: что сделать, чтобы оно наполнилось. */
export function Steps({ children, mark }: StepsProps): ReactElement {
  return (
    <ol className="steps" data-testid={mark}>
      {children}
    </ol>
  );
}
