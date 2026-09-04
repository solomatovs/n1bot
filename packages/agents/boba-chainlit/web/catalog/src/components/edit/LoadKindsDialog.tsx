import { Pencil, Plus, Trash2 } from "lucide-react";
import { useState, type FormEvent, type ReactElement } from "react";

import type { Catalog, ColumnSide, LoadField, LoadFieldType, LoadKind } from "../../model/catalog";
import type { EditActions } from "../../model/editing";
import { blankLoadKind } from "../../model/ops";
import {
  Button,
  Cell,
  Chip,
  DataTable,
  Dialog,
  Field,
  Form,
  IconButton,
  Input,
  List,
  ListAside,
  ListName,
  ListNote,
  ListRow,
  Note,
  Row,
  SectionHead,
  Select,
  TableRow,
  TextArea,
  Toolbar,
} from "../../ui";

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
  const [editingKind, setEditingKind] = useState<{
    kind: LoadKind;
    fresh: boolean;
  } | null>(null);

  if (editingKind !== null && editing !== undefined) {
    return (
      <Dialog title={editingKind.fresh ? "new load kind" : "load kind"} mark="load-kind-form" wide onClose={onClose}>
        <LoadKindForm
          kind={editingKind.kind}
          onSave={(kind) => {
            editing.apply([
              editingKind.fresh ? { op: "add_load_kind", load_kind: kind } : { op: "set_load_kind", load_kind: kind },
            ]);
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
      <List kind="cards" mark="load-kinds-list" empty="no load kinds yet">
        {catalog.loadKinds.map((kind) => {
          const used = catalog.flows.filter((flow) => flow.load.kind_id === kind.id).length;
          return (
            <ListRow key={kind.id} data-kind={kind.name}>
              <Row>
                <ListName strong>{kind.name}</ListName>
                <ListAside>
                  <Chip tone="muted">{used} flow(s)</Chip>
                  {editing !== undefined && (
                    <IconButton
                      size="sm"
                      ghost
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
                      size="sm"
                      ghost
                      aria-label={`remove load kind ${kind.name}`}
                      onClick={() => {
                        editing.apply([{ op: "remove_load_kind", id: kind.id }]);
                      }}
                    >
                      <Trash2 size={12} />
                    </IconButton>
                  )}
                </ListAside>
              </Row>
              {kind.description !== "" && <ListNote>{kind.description}</ListNote>}
              <List>
                {kind.fields.map((field) => (
                  <li key={field.name} data-field={field.name}>
                    <Note micro mono tone="faint">
                      {field.name} · {field.type}
                      {(field.type === "column" || field.type === "columns") && ` · ${field.side}`}
                      {field.required && " · required"}
                    </Note>
                  </li>
                ))}
              </List>
            </ListRow>
          );
        })}
      </List>
      {editing !== undefined && (
        <Toolbar>
          <Button
            tone="primary"
            icon={Plus}
            onClick={() => {
              setEditingKind({ kind: blankLoadKind(""), fresh: true });
            }}
          >
            load kind
          </Button>
        </Toolbar>
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
    onSave({
      ...kind,
      name: name.trim(),
      description: description.trim(),
      fields: cleaned,
    });
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
    <Form onSubmit={submit} mark="load-kind-form">
      <Field label="name" required>
        <Input
          mono
          fill
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
          fill
          aria-label="load kind description"
          rows={2}
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
        />
      </Field>
      <SectionHead
        actions={
          <Button
            size="sm"
            icon={Plus}
            onClick={() => {
              setFields((current) => [
                ...current,
                {
                  name: "",
                  type: "text",
                  side: "any",
                  required: false,
                  description: "",
                },
              ]);
            }}
          >
            field
          </Button>
        }
      >
        fields · {fields.length}
      </SectionHead>
      <div data-testid="load-kind-fields">
        <DataTable editor head={["name", "type", "side", "required", "hint", ""]}>
          {fields.map((field, index) => (
            <TableRow key={index} data-index={index}>
              <Cell>
                <Input
                  mono
                  aria-label={`field ${index} name`}
                  placeholder="name"
                  value={field.name}
                  onChange={(event) => {
                    patch(index, { name: event.target.value });
                  }}
                />
              </Cell>
              <Cell>
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
              </Cell>
              <Cell>
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
              </Cell>
              <Cell>
                <Field check>
                  <input
                    type="checkbox"
                    aria-label={`field ${index} required`}
                    checked={field.required}
                    onChange={(event) => {
                      patch(index, { required: event.target.checked });
                    }}
                  />
                </Field>
              </Cell>
              <Cell>
                <Input
                  aria-label={`field ${index} description`}
                  placeholder="hint"
                  value={field.description}
                  onChange={(event) => {
                    patch(index, { description: event.target.value });
                  }}
                />
              </Cell>
              <Cell>
                <IconButton
                  aria-label={`remove field ${index}`}
                  onClick={() => {
                    setFields((current) => current.filter((_item, at) => at !== index));
                  }}
                >
                  <Trash2 size={12} />
                </IconButton>
              </Cell>
            </TableRow>
          ))}
        </DataTable>
      </div>
      <Toolbar>
        <Button tone="primary" type="submit" disabled={name.trim() === "" || duplicated}>
          save load kind
        </Button>
        <Button tone="ghost" onClick={onCancel}>
          cancel
        </Button>
      </Toolbar>
    </Form>
  );
}
