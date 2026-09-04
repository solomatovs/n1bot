import { ArrowLeft, ArrowRight, KeyRound, Pencil, Plus, Trash2, X } from "lucide-react";
import { useState, type ReactElement } from "react";

import type { Catalog, Dataset, Flow } from "../model/catalog";
import type { EditActions } from "../model/editing";
import { Button, Chip, Eyebrow, IconButton } from "../ui";
import { ColumnsEditor } from "./edit/ColumnsEditor";
import { DatasetForm } from "./edit/DatasetForm";

type Props = {
  catalog: Catalog;
  dataset: Dataset;
  showDiff: boolean;
  /** Действия черновика; без них панель только показывает. */
  editing: EditActions | undefined;
  onActivate: (datasetId: string) => void;
  onClose: () => void;
};

type Mode = "view" | "dataset" | "columns";

/** Панель набора: паспорт, колонки, потоки в обе стороны с правилом загрузки.
 * Перенос TableDetail и RelatedTables из liam erd-core под каталог. */
export function DetailPanel({ catalog, dataset, showDiff, editing, onActivate, onClose }: Props): ReactElement {
  const [mode, setMode] = useState<Mode>("view");
  const columns = catalog.columnsOf(dataset.id);
  const flows = catalog.flowsOf(dataset.id);
  const status = catalog.statusOf("dataset", dataset.id);
  const layer = catalog.layer(dataset.layer_id);
  const referenced = referencedColumns(catalog, [...flows.incoming, ...flows.outgoing]);

  if (editing !== undefined && mode === "dataset") {
    return (
      <div className="detail" data-testid="detail-panel" data-dataset={dataset.name} data-mode={mode}>
        <DatasetForm
          dataset={dataset}
          layers={catalog.layers}
          onSave={(saved) => {
            editing.apply([{ op: "set_dataset", dataset: saved }]);
            setMode("view");
          }}
          onCancel={() => {
            setMode("view");
          }}
        />
      </div>
    );
  }

  return (
    <div className="detail" data-testid="detail-panel" data-dataset={dataset.name}>
      <header className="detail__head">
        <div className="detail__title">
          <Eyebrow>{layer?.name ?? "—"}</Eyebrow>
          <h2 className="detail__name">{dataset.name}</h2>
        </div>
        {showDiff && status !== "unchanged" && <Chip tone="draft">{status}</Chip>}
        {editing !== undefined && (
          <IconButton
            aria-label="edit dataset"
            onClick={() => {
              setMode("dataset");
            }}
          >
            <Pencil size={16} />
          </IconButton>
        )}
        {editing !== undefined && (
          <IconButton
            aria-label="remove dataset"
            onClick={() => {
              editing.removeDataset(dataset);
            }}
          >
            <Trash2 size={16} />
          </IconButton>
        )}
        <IconButton aria-label="close details" onClick={onClose}>
          <X size={16} />
        </IconButton>
      </header>

      <section className="detail__section">
        <dl className="detail__facts">
          <dt>source</dt>
          <dd>{dataset.source === "" ? "—" : dataset.source}</dd>
          <dt>owner</dt>
          <dd>{dataset.owner === "" ? "—" : dataset.owner}</dd>
          <dt>tags</dt>
          <dd>
            {dataset.tags.length === 0 ? "—" : dataset.tags.map((tag) => <Chip key={tag} tone="muted">{tag}</Chip>)}
          </dd>
        </dl>
        {dataset.description !== "" && <p className="detail__description">{dataset.description}</p>}
      </section>

      <section className="detail__section" data-testid="detail-columns">
        <div className="detail__section-head">
          <Eyebrow as="h4">columns · {columns.length}</Eyebrow>
          {editing !== undefined && mode !== "columns" && (
            <Button
              size="tiny"
              icon={Pencil}
              onClick={() => {
                setMode("columns");
              }}
            >
              edit columns
            </Button>
          )}
        </div>
        {editing !== undefined && mode === "columns" && (
          <ColumnsEditor
            datasetId={dataset.id}
            columns={columns}
            referenced={referenced}
            onSave={(ops) => {
              if (ops.length > 0) {
                editing.apply(ops);
              }
              setMode("view");
            }}
            onCancel={() => {
              setMode("view");
            }}
          />
        )}
        {mode !== "columns" && (
        <table className="detail__table">
          <tbody>
            {columns.map((column) => (
              <tr
                key={column.id}
                id={`dataset__${dataset.id}__column__${column.id}`}
                data-status={showDiff ? catalog.statusOf("column", column.id) : "unchanged"}
              >
                <td className="detail__icon">{column.is_key && <KeyRound size={11} />}</td>
                <td className="detail__col-name">{column.name}</td>
                <td className="detail__col-type">{column.type}</td>
                <td className="detail__col-null">{column.nullable ? "null" : "not null"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        )}
      </section>

      <FlowList
        title="incoming"
        icon={<ArrowLeft size={12} />}
        flows={flows.incoming}
        catalog={catalog}
        other={(flow) => flow.from_dataset_id}
        showDiff={showDiff}
        editing={editing}
        onActivate={onActivate}
      />
      <FlowList
        title="outgoing"
        icon={<ArrowRight size={12} />}
        flows={flows.outgoing}
        catalog={catalog}
        other={(flow) => flow.to_dataset_id}
        showDiff={showDiff}
        editing={editing}
        onActivate={onActivate}
        onAdd={() => {
          editing?.newFlow(dataset);
        }}
      />
    </div>
  );
}

/** Колонки, на которые ссылаются значения загрузки потоков набора. */
function referencedColumns(catalog: Catalog, flows: Flow[]): ReadonlySet<string> {
  const ids = new Set<string>();
  for (const flow of flows) {
    for (const value of Object.values(flow.load.values)) {
      if (Array.isArray(value)) {
        value.forEach((id) => ids.add(id));
      } else if (typeof value === "string" && catalog.column(value) !== undefined) {
        ids.add(value);
      }
    }
  }

  return ids;
}

type FlowListProps = {
  title: string;
  icon: ReactElement;
  flows: Flow[];
  catalog: Catalog;
  other: (flow: Flow) => string;
  showDiff: boolean;
  editing: EditActions | undefined;
  onActivate: (datasetId: string) => void;
  onAdd?: (() => void) | undefined;
};

function FlowList({ title, icon, flows, catalog, other, showDiff, editing, onActivate, onAdd }: FlowListProps): ReactElement {
  return (
    <section className="detail__section" data-testid={`detail-${title}`}>
      <div className="detail__section-head">
        <Eyebrow as="h4">
          {title} · {flows.length}
        </Eyebrow>
        {editing !== undefined && onAdd !== undefined && (
          <Button size="tiny" icon={Plus} onClick={onAdd}>
            flow
          </Button>
        )}
      </div>
      {flows.length === 0 && <p className="detail__empty">none</p>}
      <ul className="detail__flows">
        {flows.map((flow) => {
          const otherId = other(flow);
          const neighbour = catalog.dataset(otherId);
          const status = showDiff ? catalog.statusOf("flow", flow.id) : "unchanged";
          return (
            <li key={flow.id} className="detail__flow" data-status={status}>
              <button
                type="button"
                className="detail__flow-target"
                onClick={() => {
                  onActivate(otherId);
                }}
              >
                {icon}
                <span>{neighbour?.name ?? otherId}</span>
              </button>
              <Chip>{catalog.loadKindName(flow)}</Chip>
              {editing !== undefined && (
                <IconButton
                  className="detail__flow-edit"
                  aria-label={`edit flow to ${neighbour?.name ?? otherId}`}
                  onClick={() => {
                    editing.editFlow(flow);
                  }}
                >
                  <Pencil size={12} />
                </IconButton>
              )}
              <dl className="detail__values">
                {catalog.loadValues(flow).map((value) => (
                  <div key={value.field} className="detail__value">
                    <dt>{value.field}</dt>
                    <dd>{value.text}</dd>
                  </div>
                ))}
              </dl>
              {flow.description !== "" && <p className="detail__description">{flow.description}</p>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
