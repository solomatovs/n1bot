import { Plus, Trash2, Wand2 } from "lucide-react";
import { useMemo, useState, type FormEvent, type ReactElement } from "react";

import type { Catalog, ColumnLink, Flow, NodeColumn } from "../../model/catalog";
import {
  Button,
  Cell,
  DataTable,
  Field,
  Form,
  IconButton,
  Input,
  Note,
  Select,
  TableRow,
  TextArea,
  Toolbar,
  ToolbarSpacer,
} from "../../../ui";

type Props = {
  catalog: Catalog;
  flow: Flow;
  /** Поток из панели узла: приёмник выбирается здесь; с холста он уже известен. */
  pickTarget?: boolean;
  onSave: (flow: Flow) => void;
  onCancel: () => void;
  onDelete?: (() => void) | undefined;
};

/** Поток между узлами: какие колонки источника переходят в какие колонки
 * приёмника. Пары выбираются из колонок привязанных версий; у узла без
 * известных колонок имя вводится руками. «match by name» подбирает пары по
 * одинаковым именам. */
export function FlowForm({ catalog, flow, pickTarget = false, onSave, onCancel, onDelete }: Props): ReactElement {
  const [links, setLinks] = useState<ColumnLink[]>(flow.columns);
  const [description, setDescription] = useState(flow.description);
  const [targetId, setTargetId] = useState(flow.to_node_id);

  const source = catalog.label(flow.from_node_id);
  const target = targetId === "" ? "…" : catalog.label(targetId);
  const sourceColumns = useMemo(() => catalog.columnsOf(flow.from_node_id), [catalog, flow.from_node_id]);
  const targetColumns = useMemo(() => catalog.columnsOf(targetId), [catalog, targetId]);
  const targets = catalog.nodes.filter((node) => node.id !== flow.from_node_id);
  const repeated = duplicates(links);
  const incomplete = links.some((link) => link.from_column === "" || link.to_column === "");

  const setLink = (index: number, patch: Partial<ColumnLink>): void => {
    setLinks((current) => current.map((link, at) => (at === index ? { ...link, ...patch } : link)));
  };

  const removeLink = (index: number): void => {
    setLinks((current) => current.filter((_link, at) => at !== index));
  };

  const matchByName = (): void => {
    const names = new Set(targetColumns.map((column) => column.name));
    const taken = new Set(links.map((link) => `${link.from_column}->${link.to_column}`));
    const added: ColumnLink[] = [];
    for (const column of sourceColumns) {
      if (!names.has(column.name) || taken.has(`${column.name}->${column.name}`)) {
        continue;
      }

      added.push({ from_column: column.name, to_column: column.name });
    }

    setLinks((current) => [...current, ...added]);
  };

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    onSave({ ...flow, to_node_id: targetId, columns: links, description: description.trim() });
  };

  return (
    <Form onSubmit={submit} mark="flow-form">
      <Note mono>
        {source} → {target}
      </Note>
      {pickTarget && (
        <Field label="to node" required>
          <Select
            fill
            value={targetId}
            aria-label="flow target"
            onChange={(event) => {
              setTargetId(event.target.value);
              setLinks([]);
            }}
          >
            <option value="">— choose —</option>
            {targets.map((node) => (
              <option key={node.id} value={node.id}>
                {catalog.label(node.id)}
              </option>
            ))}
          </Select>
        </Field>
      )}
      <Field label={`columns · ${links.length}`} hint="which columns of the source go into which columns of the target">
        <div data-testid="flow-columns">
          {links.length > 0 && (
            <DataTable>
              {links.map((link, index) => (
                <TableRow key={index} data-pair={index}>
                  <Cell data-col="from">
                    <ColumnPick
                      label={`from column ${index}`}
                      columns={sourceColumns}
                      value={link.from_column}
                      onChange={(value) => {
                        setLink(index, { from_column: value });
                      }}
                    />
                  </Cell>
                  <Cell mod="dim">→</Cell>
                  <Cell data-col="to">
                    <ColumnPick
                      label={`to column ${index}`}
                      columns={targetColumns}
                      value={link.to_column}
                      onChange={(value) => {
                        setLink(index, { to_column: value });
                      }}
                    />
                  </Cell>
                  <Cell mod="icon">
                    <IconButton
                      size="sm"
                      ghost
                      aria-label={`remove pair ${index}`}
                      onClick={() => {
                        removeLink(index);
                      }}
                    >
                      <Trash2 size={12} />
                    </IconButton>
                  </Cell>
                </TableRow>
              ))}
            </DataTable>
          )}
          <Toolbar>
            <Button
              size="sm"
              icon={Plus}
              aria-label="add column pair"
              disabled={targetId === ""}
              onClick={() => {
                setLinks((current) => [...current, { from_column: "", to_column: "" }]);
              }}
              data-testid="add-pair"
            >
              pair
            </Button>
            <Button
              size="sm"
              icon={Wand2}
              disabled={targetId === "" || sourceColumns.length === 0 || targetColumns.length === 0}
              onClick={matchByName}
              data-testid="match-by-name"
            >
              match by name
            </Button>
          </Toolbar>
          {repeated.length > 0 && (
            <Note tone="error" mark="flow-repeated">
              repeated pairs: {repeated.join(", ")}
            </Note>
          )}
        </div>
      </Field>
      <Field label="description">
        <TextArea
          fill
          value={description}
          aria-label="flow description"
          rows={2}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
        />
      </Field>
      <Toolbar>
        <Button tone="primary" type="submit" disabled={targetId === "" || incomplete || repeated.length > 0}>
          save flow
        </Button>
        <Button tone="ghost" onClick={onCancel}>
          cancel
        </Button>
        {onDelete !== undefined && (
          <>
            <ToolbarSpacer />
            <Button tone="danger" onClick={onDelete}>
              remove flow
            </Button>
          </>
        )}
      </Toolbar>
    </Form>
  );
}

type PickProps = {
  label: string;
  columns: NodeColumn[];
  value: string;
  onChange: (value: string) => void;
};

/** Колонка конца потока: из известных колонок узла, а без них — именем. */
function ColumnPick({ label, columns, value, onChange }: PickProps): ReactElement {
  if (columns.length === 0) {
    return (
      <Input
        mono
        fill
        aria-label={label}
        value={value}
        placeholder="column"
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
    );
  }

  return (
    <Select
      fill
      aria-label={label}
      value={value}
      onChange={(event) => {
        onChange(event.target.value);
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

function duplicates(links: ColumnLink[]): string[] {
  const seen = new Set<string>();
  const repeated: string[] = [];
  for (const link of links) {
    const key = `${link.from_column} → ${link.to_column}`;
    if (link.from_column === "" || link.to_column === "") {
      continue;
    }

    if (seen.has(key) && !repeated.includes(key)) {
      repeated.push(key);
    }

    seen.add(key);
  }

  return repeated;
}
