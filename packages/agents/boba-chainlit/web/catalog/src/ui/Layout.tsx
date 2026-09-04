import type { FormEvent, HTMLAttributes, ReactElement, ReactNode } from "react";

import "./Layout.css";

type Marked = {
  /** Метка для тестов: data-testid. */
  mark?: string | undefined;
  children?: ReactNode;
};

type PageProps = Marked & Omit<HTMLAttributes<HTMLDivElement>, "className" | "children">;

/** Страница: шапка, полоса уведомлений и тело. data-* атрибуты страницы
 * (source, version, can-edit) пробрасываются как есть.
 * Единственное место, где существуют классы `page*`. */
export function Page({ mark, children, ...rest }: PageProps): ReactElement {
  return (
    <div className="page" data-testid={mark} {...rest}>
      {children}
    </div>
  );
}

/** Полоса уведомлений между шапкой и телом: Alert'ы страницы. */
export function PageNotices({ children }: { children?: ReactNode }): ReactElement {
  return <div className="page__notices">{children}</div>;
}

type BodyProps = {
  pane: boolean;
  detail: boolean;
  children: ReactNode;
};

/** Тело страницы: колонки панели, сцены и деталей включаются флагами. */
export function PageBody({ pane, detail, children }: BodyProps): ReactElement {
  return (
    <div className="page__body" data-pane={pane} data-detail={detail}>
      {children}
    </div>
  );
}

export function Pane({ mark, children }: Marked): ReactElement {
  return (
    <aside className="page__pane" data-testid={mark}>
      {children}
    </aside>
  );
}

type SceneProps = Marked & {
  /** Текстовая сцена с прокруткой вместо холста. */
  panel?: boolean;
};

export function Scene({ mark, panel = false, children }: SceneProps): ReactElement {
  const classes = ["page__scene"];
  if (panel) {
    classes.push("page__scene--panel");
  }

  return (
    <main className={classes.join(" ")} data-testid={mark}>
      {children}
    </main>
  );
}

export function Detail({ mark, children }: Marked): ReactElement {
  return (
    <aside className="page__detail" data-testid={mark}>
      {children}
    </aside>
  );
}

type StackProps = Marked & {
  gap?: "tight" | "normal" | "loose";
};

/** Вертикальная стопка с зазором из шкалы. */
export function Stack({ gap = "normal", mark, children }: StackProps): ReactElement {
  const classes = ["stack"];
  if (gap !== "normal") {
    classes.push(`stack--${gap}`);
  }

  return (
    <div className={classes.join(" ")} data-testid={mark}>
      {children}
    </div>
  );
}

type FormProps = Marked & {
  onSubmit: (event: FormEvent) => void;
  /** Поля в ряд: короткая форма в одну строку. */
  inline?: boolean;
};

/** Форма — стопка полей с зазором из шкалы; inline — поля в ряд по низу. */
export function Form({ onSubmit, inline = false, mark, children }: FormProps): ReactElement {
  const classes = ["stack"];
  if (inline) {
    classes.push("stack--inline");
  }

  return (
    <form className={classes.join(" ")} onSubmit={onSubmit} data-testid={mark}>
      {children}
    </form>
  );
}

type RowProps = Marked & {
  wrap?: boolean;
  between?: boolean;
};

/** Ряд с зазором из шкалы; `Grow` — растяжимая ячейка ряда. */
export function Row({ wrap = false, between = false, mark, children }: RowProps): ReactElement {
  const classes = ["row"];
  if (wrap) {
    classes.push("row--wrap");
  }
  if (between) {
    classes.push("row--between");
  }

  return (
    <div className={classes.join(" ")} data-testid={mark}>
      {children}
    </div>
  );
}

export function Grow({ children }: { children?: ReactNode }): ReactElement {
  return <span className="row__grow">{children}</span>;
}

type IndexProps = Marked & Omit<HTMLAttributes<HTMLDivElement>, "className" | "children">;

/** Страница-список по центру: источники, расшаренные диаграммы. */
export function Index({ mark, children, ...rest }: IndexProps): ReactElement {
  return (
    <div className="index" data-testid={mark} {...rest}>
      {children}
    </div>
  );
}

export function IndexHead({ children }: { children: ReactNode }): ReactElement {
  return <header className="index__head">{children}</header>;
}
