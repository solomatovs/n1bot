import { Children, type HTMLAttributes, type LiHTMLAttributes, type ReactElement, type ReactNode } from "react";
import { Link } from "react-router-dom";

import "./List.css";

export type ListKind = "plain" | "spaced" | "cards" | "nav";

type ListProps = Omit<HTMLAttributes<HTMLUListElement>, "className"> & {
  kind?: ListKind;
  /** Текст вместо строк, когда список пуст. */
  empty?: ReactNode;
  mark?: string | undefined;
  children?: ReactNode;
};

/** Список строк: плотный (панель), с зазором (индекс), карточками (потоки,
 * виды загрузки) или навигация (workflow и запуски: строки-ссылки с
 * подсветкой). Состояние строки —
 * `data-active`, `data-status`, `data-hidden`, `data-stale`.
 * Единственное место, где существуют классы `rows*`. */
export function List({ kind = "plain", empty, mark, children, ...rest }: ListProps): ReactElement {
  const classes = ["rows"];
  if (kind !== "plain") {
    classes.push(`rows--${kind}`);
  }

  const rows = Children.toArray(children);
  const isEmpty = rows.length === 0;

  return (
    <ul className={classes.join(" ")} data-testid={mark} data-empty={isEmpty} {...rest}>
      {isEmpty && empty !== undefined && (
        <li className="rows__empty" data-testid="list-empty">
          {empty}
        </li>
      )}
      {rows}
    </ul>
  );
}

type RowProps = Omit<LiHTMLAttributes<HTMLLIElement>, "className"> & {
  active?: boolean | undefined;
  status?: string | undefined;
  hidden?: boolean | undefined;
  stale?: boolean | undefined;
  /** Строка под родительской: со сдвигом и отбивкой слева. */
  nested?: boolean | undefined;
  mark?: string | undefined;
  children: ReactNode;
};

export function ListRow({ active, status, hidden, stale, nested, mark, children, ...rest }: RowProps): ReactElement {
  return (
    <li
      className={nested === true ? "rows__row rows__row--nested" : "rows__row"}
      data-active={active}
      data-status={status}
      data-hidden={hidden}
      data-stale={stale}
      data-testid={mark}
      {...rest}
    >
      {children}
    </li>
  );
}

type NameProps = {
  /** Кнопка выбора; без onClick и to — просто подпись. */
  onClick?: (() => void) | undefined;
  to?: string | undefined;
  strong?: boolean;
  title?: string | undefined;
  mark?: string | undefined;
  children: ReactNode;
};

/** Имя строки: кнопка выбора, ссылка или подпись; всегда одна строка с
 * многоточием. */
export function ListName({ onClick, to, strong = false, title, mark, children }: NameProps): ReactElement {
  const classes = ["rows__name"];
  if (strong) {
    classes.push("rows__name--strong");
  }
  const className = classes.join(" ");

  if (to !== undefined) {
    return (
      <Link to={to} className={className} title={title} data-testid={mark}>
        {children}
      </Link>
    );
  }

  if (onClick !== undefined) {
    return (
      <button type="button" className={className} title={title} onClick={onClick} data-testid={mark}>
        {children}
      </button>
    );
  }

  return (
    <span className={className} title={title} data-testid={mark}>
      {children}
    </span>
  );
}

/** Хвост строки: чипы и кнопки-иконки справа от имени. */
export function ListAside({ children }: { children: ReactNode }): ReactElement {
  return <span className="rows__aside">{children}</span>;
}

type LinkProps = {
  to: string;
  onClick?: (() => void) | undefined;
  mark?: string | undefined;
  children: ReactNode;
};

/** Тело строки-ссылки в несколько строк: имя и мета под ним; клик по любой
 * части ведёт по адресу. */
export function ListLink({ to, onClick, mark, children }: LinkProps): ReactElement {
  return (
    <Link to={to} className="rows__link" onClick={onClick} data-testid={mark}>
      {children}
    </Link>
  );
}

/** Строка меты под именем: чипы, счётчики, время. */
export function ListMeta({ children }: { children: ReactNode }): ReactElement {
  return <span className="rows__meta">{children}</span>;
}

type ActionProps = {
  onClick: () => void;
  mark?: string | undefined;
  children: ReactNode;
};

/** Действие над списком пунктирной кнопкой во всю ширину: «+ New workflow». */
export function ListAction({ onClick, mark, children }: ActionProps): ReactElement {
  return (
    <button type="button" className="rows__action" onClick={onClick} data-testid={mark}>
      {children}
    </button>
  );
}
