import { Children, type HTMLAttributes, type LiHTMLAttributes, type ReactElement, type ReactNode } from "react";
import { Link } from "react-router-dom";

import "./List.css";

export type ListKind = "plain" | "spaced" | "cards" | "boxed";

type ListProps = Omit<HTMLAttributes<HTMLUListElement>, "className"> & {
  kind?: ListKind;
  /** Текст вместо строк, когда список пуст. */
  empty?: ReactNode;
  mark?: string | undefined;
  children?: ReactNode;
};

/** Список строк: плотный (панель), с зазором (индекс), карточками (потоки,
 * виды загрузки) или в рамке с прокруткой (выбор). Состояние строки —
 * `data-active`, `data-status`, `data-hidden`, `data-stale`.
 * Единственное место, где существуют классы `list*`. */
export function List({ kind = "plain", empty, mark, children, ...rest }: ListProps): ReactElement {
  const classes = ["list"];
  if (kind !== "plain") {
    classes.push(`list--${kind}`);
  }

  const rows = Children.toArray(children);
  const isEmpty = rows.length === 0;

  return (
    <ul className={classes.join(" ")} data-testid={mark} data-empty={isEmpty} {...rest}>
      {isEmpty && empty !== undefined && (
        <li className="list__empty" data-testid="list-empty">
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
  mark?: string | undefined;
  children: ReactNode;
};

export function ListRow({ active, status, hidden, stale, mark, children, ...rest }: RowProps): ReactElement {
  return (
    <li
      className="list__row"
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
  const classes = ["list__name"];
  if (strong) {
    classes.push("list__name--strong");
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
  return <span className="list__aside">{children}</span>;
}

/** Мелкая заметка внутри карточки списка. */
export function ListNote({ children }: { children: ReactNode }): ReactElement {
  return <p className="list__note">{children}</p>;
}
