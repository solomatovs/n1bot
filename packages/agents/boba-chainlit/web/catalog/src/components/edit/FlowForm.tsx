import { useMemo, useState, type FormEvent, type ReactElement } from "react";

import type { Catalog, Flow, LoadField, LoadValue } from "../../model/catalog";
import { Button, Field, Input, Select, TextArea } from "../../ui";

type Props = {
  catalog: Catalog;
  flow: Flow;
  /** Поток из панели набора: приёмник выбирается здесь; с холста он уже известен. */
  pickTarget?: boolean;
  onSave: (flow: Flow) => void;
  onCancel: () => void;
  onDelete?: (() => void) | undefined;
};

/** Правило загрузки потока: вид из каталога, поля по описанию вида (текст, число,
 * флаг, колонка, колонки обоих концов), описание. Форма строится по fields вида. */
export function FlowForm({ catalog, flow, pickTarget = false, onSave, onCancel, onDelete }: Props): ReactElement {
  const [kindId, setKindId] = useState(flow.load.kind_id);
  const [values, setValues] = useState<Record<string, LoadValue>>({ ...flow.load.values });
  const [description, setDescription] = useState(flow.description);
  const [targetId, setTargetId] = useState(flow.to_dataset_id);

  const kind = catalog.loadKind(kindId);
  const source = catalog.dataset(flow.from_dataset_id);
  const target = catalog.dataset(targetId);
  const columns = useMemo(
    () => [...catalog.columnsOf(flow.from_dataset_id), ...catalog.columnsOf(targetId)],
    [catalog, flow.from_dataset_id, targetId],
  );
  const targets = catalog.datasets.filter((dataset) => dataset.id !== flow.from_dataset_id);

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
    onSave({ ...flow, to_dataset_id: targetId, load: { kind_id: kindId, values }, description: description.trim() });
  };

  const missing = (kind?.fields ?? []).filter((field) => field.required && values[field.name] === undefined);

  return (
    <form className="form" onSubmit={submit} data-testid="flow-form">
      <p className="form__note mono">
        {source?.name ?? flow.from_dataset_id} → {target?.name ?? "…"}
      </p>
      {pickTarget && (
        <Field label="to dataset" required>
          <Select
            value={targetId}
            aria-label="flow target"
            onChange={(event) => {
              setTargetId(event.target.value);
              setValues({});
            }}
          >
            <option value="">— choose —</option>
            {targets.map((dataset) => (
              <option key={dataset.id} value={dataset.id}>
                {dataset.name}
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
          <LoadValueInput field={field} value={values[field.name]} columns={columns} onChange={set} />
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

type ColumnOption = { id: string; name: string; dataset_id: string };

type InputProps = {
  field: LoadField;
  value: LoadValue | undefined;
  columns: ColumnOption[];
  onChange: (field: string, value: LoadValue | undefined) => void;
};

/** Ввод значения поля вида по его типу; пустое значение снимает поле. */
function LoadValueInput({ field, value, columns, onChange }: InputProps): ReactElement {
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
    return (
      <Select
        aria-label={label}
        value={typeof value === "string" ? value : ""}
        onChange={(event) => {
          onChange(field.name, event.target.value === "" ? undefined : event.target.value);
        }}
      >
        <option value="">—</option>
        {columns.map((column) => (
          <option key={column.id} value={column.id}>
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
          <option key={column.id} value={column.id}>
            {column.name}
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
