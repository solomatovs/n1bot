import type { HTMLAttributes, ReactElement, ReactNode, TdHTMLAttributes } from "react";

import "./DataTable.css";

export type CellMod = "icon" | "dim" | "right" | "wrap";

type TableProps = {
  head?: ReactNode[];
  /** Таблица-редактор: поля ввода в ячейках, без линий. */
  editor?: boolean;
  mark?: string | undefined;
  children: ReactNode;
};

/** Таблица данных: строки высотой в ряд, статус строки — `data-status`.
 * Единственное место, где существует класс `table`. */
export function DataTable({ head, editor = false, mark, children }: TableProps): ReactElement {
  const classes = ["table"];
  if (editor) {
    classes.push("table--editor");
  }

  return (
    <table className={classes.join(" ")} data-testid={mark}>
      {head !== undefined && (
        <thead>
          <tr>
            {head.map((title, index) => (
              <th key={index}>{title}</th>
            ))}
          </tr>
        </thead>
      )}
      <tbody>{children}</tbody>
    </table>
  );
}

type RowProps = HTMLAttributes<HTMLTableRowElement> & {
  status?: string | undefined;
  children: ReactNode;
};

export function TableRow({ status, children, ...rest }: RowProps): ReactElement {
  return (
    <tr data-status={status} {...rest}>
      {children}
    </tr>
  );
}

type CellProps = TdHTMLAttributes<HTMLTableCellElement> & {
  mod?: CellMod | undefined;
  children?: ReactNode;
};

export function Cell({ mod, className, children, ...rest }: CellProps): ReactElement {
  const classes: string[] = [];
  if (mod !== undefined) {
    classes.push(`table__${mod}`);
  }
  if (className !== undefined) {
    classes.push(className);
  }

  return (
    <td className={classes.length > 0 ? classes.join(" ") : undefined} {...rest}>
      {children}
    </td>
  );
}
