import { Pencil, Plus, Trash2 } from "lucide-react";
import { useState, type FormEvent, type ReactElement } from "react";

import type { Catalog, ColumnSide, LoadField, LoadFieldType, LoadKind } from "../../model/catalog";
import type { EditActions } from "../../model/editing";
import { blankLoadKind } from "../../model/ops";
import { Button, Chip, Eyebrow, Field, IconButton, Input, Select, TextArea } from "../../ui";
import { Dialog } from "./Dialog";

type Props = {
  catalog: Catalog;
  /** На черновике виды создаются и правятся; без правок — только список. */
  editing: EditActions | undefined;
  onClose: () => void;
};

const FIELD_TYPES: LoadFieldType[] = ["text", "int", "bool", "column", "columns", "routine"];
const SIDES: ColumnSide[] = ["source", "target", "any"];

/** Виды загрузки процесса: список с полями, на черновике — создание, правка
 * и удаление вида; удалить можно только вид без потоков, как и на сервере. */
export function LoadKindsDialog({ catalog, editing, onClose }: Props): ReactElement {
  const [editingKind, setEditingKind] = useState<{ kind: LoadKind; fresh: boolean } | null>(null);

  if (editingKind !== null && editing !== undefined) {
    return (
      <Dialog title={editingKind.fresh ? "new load kind" : "load kind"} mark="load-kind-form" onClose={onClose}>
        <LoadKindForm
          kind={editingKind.kind}
          onSave={(kind) => {
            editing.apply([editingKind.fresh ? { op: "add_load_kind", load_kind: kind } : { op: "set_load_kind", load_kind: kind }]);
            setEditingKind(null);
          }}
          onCancel={() => {
            setEditingKind(null);
          }}
        />
      </Dialog>
    );
  }

  return (
    <Dialog title="load kinds" mark="load-kinds" onClose={onClose}>
      <ul className="kinds" data-testid="load-kinds-list">
        {catalog.loadKinds.length === 0 && <li className="choices__empty">no load kinds yet</li>}
        {catalog.loadKinds.map((kind) => {
          const used = catalog.flows.filter((flow) => flow.load.kind_id === kind.id).length;
          return (
            <li key={kind.id} className="kinds__item" data-kind={kind.name}>
              <div className="kinds__head">
                <span className="kinds__name mono">{kind.name}</span>
                <Chip tone="muted">{used} flow(s)</Chip>
                {editing !== undefined && (
                  <IconButton
                    aria-label={`edit load kind ${kind.name}`}
                    onClick={() => {
                      setEditingKind({ kind, fresh: false });
                    }}
                  >
                    <Pencil size={12} />
                  </IconButton>
                )}
                {editing !== undefined && used === 0 && (
                  <IconButton
                    aria-label={`remove load kind ${kind.name}`}
                    onClick={() => {
                      editing.apply([{ op: "remove_load_kind", id: kind.id }]);
                    }}
                  >
                    <Trash2 size={12} />
                  </IconButton>
                )}
              </div>
              {kind.description !== "" && <p className="detail__description">{kind.description}</p>}
              <ul className="kinds__fields">
                {kind.fields.map((field) => (
                  <li key={field.name} className="mono" data-field={field.name}>
                    {field.name} · {field.type}
                    {(field.type === "column" || field.type === "columns") && ` · ${field.side}`}
                    {field.required && " · required"}
                  </li>
                ))}
              </ul>
            </li>
          );
        })}
      </ul>
      {editing !== undefined && (
        <div className="form__actions">
          <Button
            size="tiny"
            tone="primary"
            icon={Plus}
            onClick={() => {
              setEditingKind({ kind: blankLoadKind(""), fresh: true });
            }}
          >
            load kind
          </Button>
        </div>
      )}
    </Dialog>
  );
}

type FormProps = {
  kind: LoadKind;
  onSave: (kind: LoadKind) => void;
  onCancel: () => void;
};

/** Имя, описание и поля вида: у каждого поля тип, сторона для колонок,
 * обязательность и подсказка. */
function LoadKindForm({ kind, onSave, onCancel }: FormProps): ReactElement {
  const [name, setName] = useState(kind.name);
  const [description, setDescription] = useState(kind.description);
  const [fields, setFields] = useState<LoadField[]>(kind.fields);

  const patch = (index: number, change: Partial<LoadField>): void => {
    setFields((current) => current.map((field, at) => (at === index ? { ...field, ...change } : field)));
  };

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    const cleaned = fields.map((field) => ({ ...field, name: field.name.trim() })).filter((field) => field.name !== "");
    onSave({ ...kind, name: name.trim(), description: description.trim(), fields: cleaned });
  };

  const names = new Set<string>();
  let duplicated = false;
  for (const field of fields) {
    const trimmed = field.name.trim();
    if (trimmed === "") {
      continue;
    }

    if (names.has(trimmed)) {
      duplicated = true;
    }
    names.add(trimmed);
  }

  return (
    <form className="form" onSubmit={submit} data-testid="load-kind-form">
      <Field label="name" required>
        <Input
          mono
          autoFocus
          aria-label="load kind name"
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </Field>
      <Field label="description">
        <TextArea
          aria-label="load kind description"
          rows={2}
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
        />
      </Field>
      <div className="detail__section-head">
        <Eyebrow as="h4">fields · {fields.length}</Eyebrow>
        <Button
          size="tiny"
          icon={Plus}
          onClick={() => {
            setFields((current) => [...current, { name: "", type: "text", side: "any", required: false, description: "" }]);
          }}
        >
          field
        </Button>
      </div>
      <ul className="kinds__editor" data-testid="load-kind-fields">
        {fields.map((field, index) => (
          <li key={index} className="kinds__row" data-index={index}>
            <Input
              mono
              aria-label={`field ${index} name`}
              placeholder="name"
              value={field.name}
              onChange={(event) => {
                patch(index, { name: event.target.value });
              }}
            />
            <Select
              aria-label={`field ${index} type`}
              value={field.type}
              onChange={(event) => {
                patch(index, { type: event.target.value as LoadFieldType });
              }}
            >
              {FIELD_TYPES.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </Select>
            <Select
              aria-label={`field ${index} side`}
              value={field.side}
              disabled={field.type !== "column" && field.type !== "columns"}
              onChange={(event) => {
                patch(index, { side: event.target.value as ColumnSide });
              }}
            >
              {SIDES.map((side) => (
                <option key={side} value={side}>
                  {side}
                </option>
              ))}
            </Select>
            <label className="kinds__check">
              <input
                type="checkbox"
                aria-label={`field ${index} required`}
                checked={field.required}
                onChange={(event) => {
                  patch(index, { required: event.target.checked });
                }}
              />
              required
            </label>
            <Input
              aria-label={`field ${index} description`}
              placeholder="hint"
              value={field.description}
              onChange={(event) => {
                patch(index, { description: event.target.value });
              }}
            />
            <IconButton
              aria-label={`remove field ${index}`}
              onClick={() => {
                setFields((current) => current.filter((_item, at) => at !== index));
              }}
            >
              <Trash2 size={12} />
            </IconButton>
          </li>
        ))}
      </ul>
      <div className="form__actions">
        <Button tone="primary" type="submit" disabled={name.trim() === "" || duplicated}>
          save load kind
        </Button>
        <Button tone="ghost" onClick={onCancel}>
          cancel
        </Button>
      </div>
    </form>
  );
}
