import { ArrowLeft, ArrowRight, Crosshair, KeyRound, Pencil, Plus, Trash2, TriangleAlert, X } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactElement } from "react";

import { ApiError, type CatalogApi } from "../api/client";
import { renderRef, type Catalog, type Flow, type ObjectCard, type ProcessNode, type Stale } from "../model/catalog";
import type { EditActions } from "../model/editing";
import { Alert, Button, Chip, Eyebrow, Field, IconButton, Input, Select, TextArea } from "../ui";
import { ObjectCardPanel } from "./sources/ObjectCardPanel";

/** Откуда брать карточку объекта: по адресу и привязке либо через вид,
 * которому не нужны права на каталог. */
export type CardSource = { kind: "pinned" } | { kind: "view"; viewId: string };

type Props = {
  api: CatalogApi;
  catalog: Catalog;
  node: ProcessNode;
  cardSource: CardSource;
  showDiff: boolean;
  /** Действия черновика; без них панель только показывает. */
  editing: EditActions | undefined;
  /** Узел ждёт нового адреса: следующий выбранный объект дерева станет его целью. */
  retargeting: boolean;
  onRetargetToggle: () => void;
  onActivate: (nodeId: string) => void;
  onClose: () => void;
};

type Mode = "view" | "node";
type CardState = { status: "loading" } | { status: "failed"; message: string } | { status: "card"; card: ObjectCard };

/** Панель узла: слой, подпись и адрес, колонки из привязанной версии, причины
 * устаревания, потоки в обе стороны с правилом загрузки и родная карточка
 * объекта из источника. */
export function DetailPanel({
  api,
  catalog,
  node,
  cardSource,
  showDiff,
  editing,
  retargeting,
  onRetargetToggle,
  onActivate,
  onClose,
}: Props): ReactElement {
  const [mode, setMode] = useState<Mode>("view");
  const columns = catalog.columnsOf(node.id);
  const flows = catalog.flowsOf(node.id);
  const status = catalog.statusOf("node", node.id);
  const stale = catalog.staleOf("node", node.id);
  const layer = catalog.layer(node.layer_id);
  const label = catalog.label(node.id);
  const address = renderRef(node.ref);

  if (editing !== undefined && mode === "node") {
    return (
      <div className="detail" data-testid="detail-panel" data-node={address} data-mode={mode}>
        <NodeForm
          catalog={catalog}
          node={node}
          onSave={(saved) => {
            editing.apply([{ op: "set_node", node: saved }]);
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
    <div className="detail" data-testid="detail-panel" data-node={address} data-stale={stale.length > 0}>
      <header className="detail__head">
        <div className="detail__title">
          <Eyebrow>{layer?.name ?? "—"}</Eyebrow>
          <h2 className="detail__name">{label}</h2>
        </div>
        {showDiff && status !== "unchanged" && <Chip tone="draft">{status}</Chip>}
        {editing !== undefined && (
          <IconButton
            aria-label="edit node"
            onClick={() => {
              setMode("node");
            }}
          >
            <Pencil size={16} />
          </IconButton>
        )}
        {editing !== undefined && (
          <IconButton
            aria-label={retargeting ? "stop retargeting" : "retarget node"}
            aria-pressed={retargeting}
            onClick={onRetargetToggle}
          >
            <Crosshair size={16} />
          </IconButton>
        )}
        {editing !== undefined && (
          <IconButton
            aria-label="remove node"
            onClick={() => {
              editing.removeNode(node);
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
          <dt>object</dt>
          <dd className="mono" data-testid="node-address">
            {address}
          </dd>
          <dt>kind</dt>
          <dd>{node.ref.kind}</dd>
          <dt>pinned</dt>
          <dd>{pinnedText(catalog.context.pins[node.ref.source_id])}</dd>
        </dl>
        {node.note !== "" && <p className="detail__description">{node.note}</p>}
        {retargeting && (
          <Alert tone="info" mark="retarget-hint">
            Pick an object in the sources tree: the node will point at it, its flows stay.
          </Alert>
        )}
      </section>

      {stale.length > 0 && <StaleList entries={stale} />}

      <section className="detail__section" data-testid="detail-columns">
        <Eyebrow as="h4">columns · {columns.length}</Eyebrow>
        {columns.length === 0 && <p className="detail__empty">none</p>}
        {columns.length > 0 && (
          <table className="detail__table">
            <tbody>
              {columns.map((column) => (
                <tr key={column.name} data-column={column.name}>
                  <td className="detail__icon">{column.key && <KeyRound size={11} />}</td>
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
        other={(flow) => flow.from_node_id}
        showDiff={showDiff}
        editing={editing}
        onActivate={onActivate}
      />
      <FlowList
        title="outgoing"
        icon={<ArrowRight size={12} />}
        flows={flows.outgoing}
        catalog={catalog}
        other={(flow) => flow.to_node_id}
        showDiff={showDiff}
        editing={editing}
        onActivate={onActivate}
        onAdd={() => {
          editing?.newFlow(node);
        }}
      />

      <SourceCard api={api} catalog={catalog} node={node} cardSource={cardSource} />
    </div>
  );
}

function pinnedText(version: number | undefined): string {
  return version === undefined ? "latest" : `v${version}`;
}

/** Причины устаревания узла или потока: что изменилось в источнике после
 * привязанной версии. */
export function StaleList({ entries }: { entries: Stale[] }): ReactElement {
  return (
    <section className="detail__section detail__stale" data-testid="detail-stale">
      <Eyebrow as="h4">
        <TriangleAlert size={12} /> stale · {entries.length}
      </Eyebrow>
      <ul className="detail__stale-list">
        {entries.map((entry, index) => (
          <li key={`${entry.reason}-${index}`} className="mono" data-reason={entry.reason}>
            <span>{entry.reason.replaceAll("_", " ")}</span>
            <span className="detail__stale-versions">
              v{entry.pinned_version} → v{entry.since_version}
            </span>
            {Object.entries(entry.detail).map(([key, value]) => (
              <span key={key} className="detail__stale-detail">
                {key}: {value}
              </span>
            ))}
          </li>
        ))}
      </ul>
    </section>
  );
}

type CardProps = { api: CatalogApi; catalog: Catalog; node: ProcessNode; cardSource: CardSource };

/** Родная карточка объекта из привязанной версии источника, ниже фактов узла. */
function SourceCard({ api, catalog, node, cardSource }: CardProps): ReactElement {
  const [state, setState] = useState<CardState>({ status: "loading" });
  const ref = node.ref;
  const version = catalog.context.pins[ref.source_id] ?? -1;
  const viewId = cardSource.kind === "view" ? cardSource.viewId : undefined;

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    const loading =
      viewId === undefined
        ? api.sourceObject(ref.source_id, version, ref.kind, ref.path)
        : api.viewObject(viewId, node.id);
    loading
      .then((card) => {
        if (!cancelled) {
          setState({ status: "card", card });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({ status: "failed", message: error instanceof ApiError ? error.detail : String(error) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, ref, version, viewId, node.id]);

  if (state.status === "loading") {
    return <p className="detail__empty">loading the source object…</p>;
  }

  if (state.status === "failed") {
    return (
      <Alert tone="error" mark="node-card-missing">
        {state.message}
      </Alert>
    );
  }

  return (
    <section className="detail__section detail__source-card" data-testid="node-card">
      <Eyebrow as="h4">in the source</Eyebrow>
      <ObjectCardPanel card={state.card} />
    </section>
  );
}

type FormProps = {
  catalog: Catalog;
  node: ProcessNode;
  onSave: (node: ProcessNode) => void;
  onCancel: () => void;
};

/** Правка узла: слой, alias и заметка; адрес меняет перенацеливание. */
function NodeForm({ catalog, node, onSave, onCancel }: FormProps): ReactElement {
  const [layerId, setLayerId] = useState(node.layer_id);
  const [alias, setAlias] = useState(node.alias ?? "");
  const [note, setNote] = useState(node.note);

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    const trimmed = alias.trim();
    onSave({ ...node, layer_id: layerId, alias: trimmed === "" ? null : trimmed, note: note.trim() });
  };

  return (
    <form className="form" onSubmit={submit} data-testid="node-form">
      <p className="form__note mono">{renderRef(node.ref)}</p>
      <Field label="layer" required>
        <Select
          value={layerId}
          aria-label="node layer"
          onChange={(event) => {
            setLayerId(event.target.value);
          }}
        >
          {catalog.layers.map((layer) => (
            <option key={layer.id} value={layer.id}>
              {layer.name}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="alias" hint="shown instead of the object name">
        <Input
          mono
          aria-label="node alias"
          value={alias}
          onChange={(event) => {
            setAlias(event.target.value);
          }}
        />
      </Field>
      <Field label="note">
        <TextArea
          aria-label="node note"
          rows={3}
          value={note}
          onChange={(event) => {
            setNote(event.target.value);
          }}
        />
      </Field>
      <div className="form__actions">
        <Button tone="primary" type="submit">
          save node
        </Button>
        <Button tone="ghost" onClick={onCancel}>
          cancel
        </Button>
      </div>
    </form>
  );
}

type FlowListProps = {
  title: string;
  icon: ReactElement;
  flows: Flow[];
  catalog: Catalog;
  other: (flow: Flow) => string;
  showDiff: boolean;
  editing: EditActions | undefined;
  onActivate: (nodeId: string) => void;
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
          const neighbour = catalog.label(otherId);
          const status = showDiff ? catalog.statusOf("flow", flow.id) : "unchanged";
          const stale = catalog.staleOf("flow", flow.id);
          return (
            <li key={flow.id} className="detail__flow" data-status={status} data-stale={stale.length > 0}>
              <button
                type="button"
                className="detail__flow-target"
                onClick={() => {
                  onActivate(otherId);
                }}
              >
                {icon}
                <span>{neighbour}</span>
              </button>
              <Chip>{catalog.loadKindName(flow)}</Chip>
              {stale.length > 0 && (
                <Chip tone="draft">
                  <TriangleAlert size={10} /> stale
                </Chip>
              )}
              {editing !== undefined && (
                <IconButton
                  className="detail__flow-edit"
                  aria-label={`edit flow to ${neighbour}`}
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
              {stale.length > 0 && <StaleList entries={stale} />}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
