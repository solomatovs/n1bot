import type { HTMLAttributes, ReactElement, ReactNode, TdHTMLAttributes } from "react";

import "./DataTable.css";

export type CellMod = "icon" | "dim" | "wrap";

type TableProps = {
  head?: ReactNode[];
  mark?: string | undefined;
  children: ReactNode;
};

/** Таблица данных: строки высотой в ряд, статус строки — `data-status`.
 * Таблица шире родителя прокручивается по горизонтали внутри своей обёртки,
 * а не раздвигает панель или окно. Единственное место, где существует
 * класс `table`. */
export function DataTable({ head, mark, children }: TableProps): ReactElement {
  return (
    <div className="table-scroll" data-testid={mark}>
      <table className="table">
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
    </div>
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
