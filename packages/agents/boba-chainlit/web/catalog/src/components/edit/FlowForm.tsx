import { useMemo, useState, type FormEvent, type ReactElement } from "react";

import { renderRef, type Catalog, type Flow, type LoadField, type LoadValue, type NodeColumn, type ObjectRef } from "../../model/catalog";
import { Button, Field, Input, Select, TextArea } from "../../ui";

type Props = {
  catalog: Catalog;
  flow: Flow;
  /** Поток из панели узла: приёмник выбирается здесь; с холста он уже известен. */
  pickTarget?: boolean;
  onSave: (flow: Flow) => void;
  onCancel: () => void;
  onDelete?: (() => void) | undefined;
};

/** Правило загрузки потока: вид из процесса, поля по описанию вида — текст,
 * число, флаг, колонки того конца, что задан стороной поля, рутина из
 * узлов-рутин процесса; описание. Форма строится по fields вида. */
export function FlowForm({ catalog, flow, pickTarget = false, onSave, onCancel, onDelete }: Props): ReactElement {
  const [kindId, setKindId] = useState(flow.load.kind_id);
  const [values, setValues] = useState<Record<string, LoadValue>>({ ...flow.load.values });
  const [description, setDescription] = useState(flow.description);
  const [targetId, setTargetId] = useState(flow.to_node_id);

  const kind = catalog.loadKind(kindId);
  const source = catalog.label(flow.from_node_id);
  const target = targetId === "" ? "…" : catalog.label(targetId);
  const sourceColumns = useMemo(() => catalog.columnsOf(flow.from_node_id), [catalog, flow.from_node_id]);
  const targetColumns = useMemo(() => catalog.columnsOf(targetId), [catalog, targetId]);
  const targets = catalog.nodes.filter((node) => node.id !== flow.from_node_id);
  const routines = catalog.routineNodes();

  const set = (field: string, value: LoadValue | undefined): void => {
    setValues((current) => {
      const next: Record<string, LoadValue> = {};
      for (const [name, kept] of Object.entries(current)) {
        if (name !== field) {
          next[name] = kept;
        }
      }
      if (value !== undefined) {
        next[field] = value;
      }
      return next;
    });
  };

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    onSave({ ...flow, to_node_id: targetId, load: { kind_id: kindId, values }, description: description.trim() });
  };

  const missing = (kind?.fields ?? []).filter((field) => field.required && values[field.name] === undefined);

  const columnsFor = (field: LoadField): NodeColumn[] => {
    if (field.side === "source") {
      return sourceColumns;
    }

    if (field.side === "target") {
      return targetColumns;
    }

    return [...sourceColumns, ...targetColumns];
  };

  return (
    <form className="form" onSubmit={submit} data-testid="flow-form">
      <p className="form__note mono">
        {source} → {target}
      </p>
      {pickTarget && (
        <Field label="to node" required>
          <Select
            value={targetId}
            aria-label="flow target"
            onChange={(event) => {
              setTargetId(event.target.value);
              setValues({});
            }}
          >
            <option value="">— choose —</option>
            {targets.map((node) => (
              <option key={node.id} value={node.id}>
                {catalog.layer(node.layer_id)?.name ?? "?"} / {catalog.label(node.id)}
              </option>
            ))}
          </Select>
        </Field>
      )}
      <Field label="load kind" required>
        <Select
          value={kindId}
          aria-label="load kind"
          onChange={(event) => {
            setKindId(event.target.value);
            setValues({});
          }}
        >
          <option value="">— choose —</option>
          {catalog.loadKinds.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </Select>
      </Field>
      {kind?.fields.map((field) => (
        <Field key={field.name} label={field.name} required={field.required} hint={field.description || undefined}>
          <LoadValueInput
            field={field}
            value={values[field.name]}
            columns={columnsFor(field)}
            routines={routines.map((node) => ({ ref: node.ref, label: catalog.label(node.id) }))}
            onChange={set}
          />
        </Field>
      ))}
      <Field label="description">
        <TextArea
          value={description}
          aria-label="flow description"
          rows={2}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
        />
      </Field>
      <div className="form__actions">
        <Button tone="primary" type="submit" disabled={kindId === "" || targetId === "" || missing.length > 0}>
          save flow
        </Button>
        <Button tone="ghost" onClick={onCancel}>
          cancel
        </Button>
        {onDelete !== undefined && (
          <>
            <span className="form__spacer" />
            <Button tone="danger" onClick={onDelete}>
              remove flow
            </Button>
          </>
        )}
      </div>
    </form>
  );
}

type RoutineOption = { ref: ObjectRef; label: string };

type InputProps = {
  field: LoadField;
  value: LoadValue | undefined;
  columns: NodeColumn[];
  routines: RoutineOption[];
  onChange: (field: string, value: LoadValue | undefined) => void;
};

/** Ввод значения поля вида по его типу; пустое значение снимает поле. */
function LoadValueInput({ field, value, columns, routines, onChange }: InputProps): ReactElement {
  const label = `load field ${field.name}`;

  if (field.type === "bool") {
    return (
      <input
        type="checkbox"
        aria-label={label}
        checked={value === true}
        onChange={(event) => {
          onChange(field.name, event.target.checked);
        }}
      />
    );
  }

  if (field.type === "int") {
    return (
      <Input
        mono
        type="number"
        aria-label={label}
        value={typeof value === "number" ? String(value) : ""}
        onChange={(event) => {
          onChange(field.name, event.target.value === "" ? undefined : Number(event.target.value));
        }}
      />
    );
  }

  if (field.type === "column") {
    const chosen = typeof value === "string" ? value : "";
    return (
      <Select
        aria-label={label}
        value={chosen}
        onChange={(event) => {
          onChange(field.name, event.target.value === "" ? undefined : event.target.value);
        }}
      >
        <option value="">—</option>
        {columns.map((column) => (
          <option key={column.name} value={column.name}>
            {column.name}
          </option>
        ))}
      </Select>
    );
  }

  if (field.type === "columns") {
    const chosen = Array.isArray(value) ? value : [];
    return (
      <Select
        multiple
        aria-label={label}
        value={chosen}
        onChange={(event) => {
          const picked = Array.from(event.target.selectedOptions, (option) => option.value);
          onChange(field.name, picked.length === 0 ? undefined : picked);
        }}
      >
        {columns.map((column) => (
          <option key={column.name} value={column.name}>
            {column.name}
          </option>
        ))}
      </Select>
    );
  }

  if (field.type === "routine") {
    const chosen = typeof value === "object" && !Array.isArray(value) ? renderRef(value) : "";
    return (
      <Select
        aria-label={label}
        value={chosen}
        onChange={(event) => {
          const picked = routines.find((routine) => renderRef(routine.ref) === event.target.value);
          onChange(field.name, picked?.ref);
        }}
      >
        <option value="">—</option>
        {routines.map((routine) => (
          <option key={renderRef(routine.ref)} value={renderRef(routine.ref)}>
            {routine.label} · {renderRef(routine.ref)}
          </option>
        ))}
      </Select>
    );
  }

  return (
    <Input
      mono
      aria-label={label}
      value={typeof value === "string" ? value : ""}
      onChange={(event) => {
        onChange(field.name, event.target.value === "" ? undefined : event.target.value);
      }}
    />
  );
}
