import { Plus, Trash2 } from "lucide-react";
import { useState, type FormEvent, type ReactElement } from "react";

import type { ManualColumn, ManualObject, SourceKind } from "../../model/catalog";
import {
  Button,
  Cell,
  DataTable,
  Dialog,
  Field,
  Form,
  IconButton,
  Input,
  Row,
  Select,
  TableRow,
  Toolbar,
} from "../../ui";

type Props = {
  kind: SourceKind;
  /** Существующий объект — правка, путь закрыт; undefined — новый. */
  initial: ManualObject | undefined;
  onSave: (object: ManualObject) => void;
  onClose: () => void;
};

/** Объект ручного источника коротким набором полей: путь родной длины (у
 * Postgres база, схема, имя; у ClickHouse база, имя), вид, комментарий,
 * колонки с типом, nullable и комментарием. */
export function ObjectForm({ kind, initial, onSave, onClose }: Props): ReactElement {
  const labels = pathLabels(kind);
  const [path, setPath] = useState<string[]>(initial?.path ?? labels.map(() => ""));
  const [objectKind, setObjectKind] = useState<ManualObject["kind"]>(initial?.kind ?? "table");
  const [comment, setComment] = useState(initial?.comment ?? "");
  const [columns, setColumns] = useState<ManualColumn[]>(initial?.columns ?? []);
  const editing = initial !== undefined;

  const setStep = (index: number, value: string): void => {
    setPath((current) => current.map((step, at) => (at === index ? value : step)));
  };

  const setColumn = (index: number, patch: Partial<ManualColumn>): void => {
    setColumns((current) => current.map((column, at) => (at === index ? { ...column, ...patch } : column)));
  };

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    const trimmedPath = path.map((step) => step.trim());
    if (trimmedPath.some((step) => step === "")) {
      return;
    }

    const trimmedColumns: ManualColumn[] = [];
    for (const column of columns) {
      trimmedColumns.push({
        name: column.name.trim(),
        type: column.type.trim(),
        nullable: column.nullable,
        comment: column.comment === null || column.comment.trim() === "" ? null : column.comment.trim(),
      });
    }

    onSave({
      kind: objectKind,
      path: trimmedPath,
      comment: comment.trim() === "" ? null : comment.trim(),
      columns: trimmedColumns,
    });
  };

  const valid =
    path.every((step) => step.trim() !== "") && columns.every((c) => c.name.trim() !== "" && c.type.trim() !== "");

  return (
    <Dialog title={editing ? "object" : "new object"} mark="object-form" wide onClose={onClose}>
      <Form onSubmit={submit} mark="object-form">
        <Row wrap>
          {labels.map((label, index) => (
            <Field key={label} label={label} required>
              <Input
                mono
                fill
                aria-label={`object ${label}`}
                value={path[index] ?? ""}
                disabled={editing}
                onChange={(event) => {
                  setStep(index, event.target.value);
                }}
              />
            </Field>
          ))}
        </Row>
        <Field label="kind">
          <Select
            aria-label="object kind"
            value={objectKind}
            onChange={(event) => {
              setObjectKind(event.target.value === "view" ? "view" : "table");
            }}
          >
            <option value="table">table</option>
            <option value="view">view</option>
          </Select>
        </Field>
        <Field label="comment">
          <Input
            fill
            aria-label="object comment"
            value={comment}
            onChange={(event) => {
              setComment(event.target.value);
            }}
          />
        </Field>
        <div data-testid="object-columns">
          <DataTable editor head={["name", "type", "null", "comment", ""]}>
            {columns.map((column, index) => (
              <TableRow key={index}>
                <Cell>
                  <Input
                    mono
                    aria-label="column name"
                    value={column.name}
                    onChange={(event) => {
                      setColumn(index, { name: event.target.value });
                    }}
                  />
                </Cell>
                <Cell>
                  <Input
                    mono
                    aria-label="column type"
                    value={column.type}
                    onChange={(event) => {
                      setColumn(index, { type: event.target.value });
                    }}
                  />
                </Cell>
                <Cell>
                  <Field check>
                    <input
                      type="checkbox"
                      aria-label={`nullable ${column.name}`}
                      checked={column.nullable}
                      onChange={(event) => {
                        setColumn(index, { nullable: event.target.checked });
                      }}
                    />
                  </Field>
                </Cell>
                <Cell>
                  <Input
                    aria-label="column comment"
                    value={column.comment ?? ""}
                    onChange={(event) => {
                      setColumn(index, { comment: event.target.value });
                    }}
                  />
                </Cell>
                <Cell>
                  <IconButton
                    aria-label={`remove column ${column.name}`}
                    onClick={() => {
                      setColumns((current) => current.filter((_, at) => at !== index));
                    }}
                  >
                    <Trash2 size={12} />
                  </IconButton>
                </Cell>
              </TableRow>
            ))}
          </DataTable>
          <Toolbar>
            <Button
              size="sm"
              icon={Plus}
              onClick={() => {
                setColumns((current) => [...current, { name: "", type: "", nullable: true, comment: null }]);
              }}
            >
              column
            </Button>
          </Toolbar>
        </div>
        <Toolbar>
          <Button tone="primary" type="submit" disabled={!valid}>
            save object
          </Button>
          <Button tone="ghost" onClick={onClose}>
            cancel
          </Button>
        </Toolbar>
      </Form>
    </Dialog>
  );
}

function pathLabels(kind: SourceKind): string[] {
  if (kind === "postgres") {
    return ["database", "schema", "name"];
  }

  return ["database", "name"];
}
