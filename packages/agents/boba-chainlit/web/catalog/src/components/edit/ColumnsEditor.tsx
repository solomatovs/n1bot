import { Plus, Trash2 } from "lucide-react";
import { useState, type ReactElement } from "react";

import type { Column } from "../../model/catalog";
import { blankColumn, type CatalogOp } from "../../model/ops";
import { Button, IconButton, Input } from "../../ui";

/** Смещение временных позиций при перестановке колонок. */
const PARKED = 10_000;

type Props = {
  datasetId: string;
  columns: Column[];
  /** Колонки, на которые ссылаются потоки: удалить их сервер не даст. */
  referenced: ReadonlySet<string>;
  onSave: (ops: CatalogOp[]) => void;
  onCancel: () => void;
};

/** Таблица колонок набора с правкой в строках; сохранение считает разницу с
 * исходным списком и отдаёт операции add/set/remove_column. */
export function ColumnsEditor({ datasetId, columns, referenced, onSave, onCancel }: Props): ReactElement {
  const [rows, setRows] = useState<Column[]>(columns);

  const change = (id: string, patch: Partial<Column>): void => {
    setRows((current) => current.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  };

  const remove = (id: string): void => {
    setRows((current) => current.filter((row) => row.id !== id));
  };

  const add = (): void => {
    setRows((current) => [...current, blankColumn(datasetId, current.length)]);
  };

  // сервер проверяет инварианты после каждой операции, поэтому порядок важен:
  // сначала удаления, затем изменённые колонки отводятся на свободные позиции,
  // затем встают на свои, и только потом добавляются новые
  const save = (): void => {
    const before = new Map(columns.map((column) => [column.id, column]));
    const final = rows.map((row, index) => ({ ...row, name: row.name.trim(), type: row.type.trim(), position: index }));
    const kept = new Set(final.map((column) => column.id));
    const changed = final.filter((column) => {
      const previous = before.get(column.id);
      return previous !== undefined && JSON.stringify(previous) !== JSON.stringify(column);
    });
    const added = final.filter((column) => !before.has(column.id));

    const ops: CatalogOp[] = [];
    for (const column of columns) {
      if (!kept.has(column.id)) {
        ops.push({ op: "remove_column", id: column.id });
      }
    }
    for (const column of changed) {
      ops.push({ op: "set_column", column: { ...column, position: PARKED + column.position } });
    }
    for (const column of changed) {
      ops.push({ op: "set_column", column });
    }
    for (const column of added) {
      ops.push({ op: "add_column", column });
    }

    onSave(ops);
  };

  const valid = rows.every((row) => row.name.trim() !== "" && row.type.trim() !== "");

  return (
    <div className="columns-editor" data-testid="columns-editor">
      <table className="detail__table">
        <thead>
          <tr>
            <th>key</th>
            <th>name</th>
            <th>type</th>
            <th>null</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} data-column={row.name}>
              <td>
                <input
                  type="checkbox"
                  aria-label={`key ${row.name}`}
                  checked={row.is_key}
                  onChange={(event) => {
                    change(row.id, { is_key: event.target.checked });
                  }}
                />
              </td>
              <td>
                <Input
                  mono
                  aria-label="column name"
                  value={row.name}
                  onChange={(event) => {
                    change(row.id, { name: event.target.value });
                  }}
                />
              </td>
              <td>
                <Input
                  mono
                  aria-label="column type"
                  value={row.type}
                  onChange={(event) => {
                    change(row.id, { type: event.target.value });
                  }}
                />
              </td>
              <td>
                <input
                  type="checkbox"
                  aria-label={`nullable ${row.name}`}
                  checked={row.nullable}
                  onChange={(event) => {
                    change(row.id, { nullable: event.target.checked });
                  }}
                />
              </td>
              <td>
                <IconButton
                  aria-label={`remove column ${row.name}`}
                  disabled={referenced.has(row.id)}
                  title={referenced.has(row.id) ? "a flow refers to this column" : undefined}
                  onClick={() => {
                    remove(row.id);
                  }}
                >
                  <Trash2 size={14} />
                </IconButton>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="form__actions">
        <Button size="tiny" icon={Plus} onClick={add}>
          column
        </Button>
        <span className="form__spacer" />
        <Button tone="primary" size="tiny" disabled={!valid} onClick={save}>
          save columns
        </Button>
        <Button tone="ghost" size="tiny" onClick={onCancel}>
          cancel
        </Button>
      </div>
    </div>
  );
}
