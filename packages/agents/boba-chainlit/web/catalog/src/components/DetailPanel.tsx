import { ArrowLeft, ArrowRight, Crosshair, KeyRound, Pencil, Plus, Trash2, TriangleAlert, X } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactElement } from "react";

import { ApiError, type CatalogApi } from "../api/client";
import { renderRef, type Catalog, type Flow, type ObjectCard, type ProcessNode, type Stale } from "../model/catalog";
import type { EditActions } from "../model/editing";
import {
  Alert,
  Button,
  Cell,
  Chip,
  DataTable,
  Facts,
  Field,
  Form,
  IconButton,
  Input,
  List,
  ListAside,
  ListName,
  ListRow,
  Note,
  Panel,
  PanelHead,
  Row,
  Section,
  SectionText,
  Select,
  TableRow,
  TextArea,
  Toolbar,
} from "../ui";
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
      <div data-testid="detail-panel" data-node={address} data-mode={mode}>
        <Panel>
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
        </Panel>
      </div>
    );
  }

  return (
    <div data-testid="detail-panel" data-node={address} data-stale={stale.length > 0}>
      <Panel>
        <PanelHead
          eyebrow={layer?.name ?? "—"}
          name={label}
          description={node.note !== "" ? node.note : undefined}
          actions={
            <>
              {showDiff && status !== "unchanged" && <Chip tone="draft">{status}</Chip>}
              {editing !== undefined && (
                <IconButton
                  size="sm"
                  ghost
                  aria-label="edit node"
                  onClick={() => {
                    setMode("node");
                  }}
                >
                  <Pencil size={14} />
                </IconButton>
              )}
              {editing !== undefined && (
                <IconButton
                  size="sm"
                  ghost
                  aria-label={retargeting ? "stop retargeting" : "retarget node"}
                  aria-pressed={retargeting}
                  onClick={onRetargetToggle}
                >
                  <Crosshair size={14} />
                </IconButton>
              )}
              {editing !== undefined && (
                <IconButton
                  size="sm"
                  ghost
                  aria-label="remove node"
                  onClick={() => {
                    editing.removeNode(node);
                  }}
                >
                  <Trash2 size={14} />
                </IconButton>
              )}
              <IconButton size="sm" ghost aria-label="close details" onClick={onClose}>
                <X size={14} />
              </IconButton>
            </>
          }
        />

        <Section>
          <Facts
            facts={[
              {
                key: "object",
                label: "object",
                value: (
                  <span className="mono" data-testid="node-address">
                    {address}
                  </span>
                ),
              },
              { key: "kind", label: "kind", value: node.ref.kind },
              {
                key: "pinned",
                label: "pinned",
                value: pinnedText(catalog.context.pins[node.ref.source_id]),
              },
            ]}
          />
          {retargeting && (
            <Alert tone="info" mark="retarget-hint">
              Pick an object in the sources tree: the node will point at it, its flows stay.
            </Alert>
          )}
        </Section>

        {stale.length > 0 && <StaleList entries={stale} />}

        <Section title={`columns · ${columns.length}`} mark="detail-columns">
          {columns.length === 0 && <Note mark="detail-empty">none</Note>}
          {columns.length > 0 && (
            <DataTable>
              {columns.map((column) => (
                <TableRow key={column.name} data-column={column.name}>
                  <Cell mod="icon">{column.key && <KeyRound size={11} />}</Cell>
                  <Cell data-col="name">{column.name}</Cell>
                  <Cell mod="dim" data-col="type">
                    {column.type}
                  </Cell>
                  <Cell mod="dim" data-col="null">
                    {column.nullable ? "null" : "not null"}
                  </Cell>
                </TableRow>
              ))}
            </DataTable>
          )}
        </Section>

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
      </Panel>
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
    <Section
      title={
        <>
          <TriangleAlert size={12} /> stale · {entries.length}
        </>
      }
      mark="detail-stale"
    >
      <List kind="cards">
        {entries.map((entry, index) => (
          <ListRow key={`${entry.reason}-${index}`} stale data-reason={entry.reason}>
            <Row wrap>
              <span className="mono">{entry.reason.replaceAll("_", " ")}</span>
              <Note micro tone="faint" mono>
                v{entry.pinned_version} → v{entry.since_version}
              </Note>
              {Object.entries(entry.detail).map(([key, value]) => (
                <Note key={key} micro tone="faint" mono>
                  {key}: {value}
                </Note>
              ))}
            </Row>
          </ListRow>
        ))}
      </List>
    </Section>
  );
}

type CardProps = {
  api: CatalogApi;
  catalog: Catalog;
  node: ProcessNode;
  cardSource: CardSource;
};

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
          setState({
            status: "failed",
            message: error instanceof ApiError ? error.detail : String(error),
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, ref, version, viewId, node.id]);

  if (state.status === "loading") {
    return (
      <Section>
        <Note mark="detail-empty">loading the source object…</Note>
      </Section>
    );
  }

  if (state.status === "failed") {
    return (
      <Section>
        <Alert tone="error" mark="node-card-missing">
          {state.message}
        </Alert>
      </Section>
    );
  }

  return (
    <Section title="in the source" mark="node-card">
      <ObjectCardPanel card={state.card} flat />
    </Section>
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
    onSave({
      ...node,
      layer_id: layerId,
      alias: trimmed === "" ? null : trimmed,
      note: note.trim(),
    });
  };

  return (
    <Form onSubmit={submit} mark="node-form">
      <Note mono>{renderRef(node.ref)}</Note>
      <Field label="layer" required>
        <Select
          fill
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
          fill
          aria-label="node alias"
          value={alias}
          onChange={(event) => {
            setAlias(event.target.value);
          }}
        />
      </Field>
      <Field label="note">
        <TextArea
          fill
          aria-label="node note"
          rows={3}
          value={note}
          onChange={(event) => {
            setNote(event.target.value);
          }}
        />
      </Field>
      <Toolbar>
        <Button tone="primary" type="submit">
          save node
        </Button>
        <Button tone="ghost" onClick={onCancel}>
          cancel
        </Button>
      </Toolbar>
    </Form>
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

function FlowList({
  title,
  icon,
  flows,
  catalog,
  other,
  showDiff,
  editing,
  onActivate,
  onAdd,
}: FlowListProps): ReactElement {
  return (
    <Section
      title={`${title} · ${flows.length}`}
      mark={`detail-${title}`}
      actions={
        editing !== undefined &&
        onAdd !== undefined && (
          <Button size="sm" icon={Plus} onClick={onAdd}>
            flow
          </Button>
        )
      }
    >
      {flows.length === 0 && <Note mark="detail-empty">none</Note>}
      {flows.length > 0 && (
        <List kind="cards">
          {flows.map((flow) => {
            const otherId = other(flow);
            const neighbour = catalog.label(otherId);
            const status = showDiff ? catalog.statusOf("flow", flow.id) : "unchanged";
            const stale = catalog.staleOf("flow", flow.id);
            const values = catalog.loadValues(flow);
            return (
              <ListRow key={flow.id} status={status} stale={stale.length > 0} mark="detail-flow" data-flow={flow.id}>
                <Row>
                  <ListName
                    onClick={() => {
                      onActivate(otherId);
                    }}
                  >
                    {icon} {neighbour}
                  </ListName>
                  <ListAside>
                    <Chip>{catalog.loadKindName(flow)}</Chip>
                    {stale.length > 0 && (
                      <Chip tone="warn">
                        <TriangleAlert size={10} /> stale
                      </Chip>
                    )}
                    {editing !== undefined && (
                      <IconButton
                        size="sm"
                        ghost
                        aria-label={`edit flow to ${neighbour}`}
                        onClick={() => {
                          editing.editFlow(flow);
                        }}
                      >
                        <Pencil size={12} />
                      </IconButton>
                    )}
                  </ListAside>
                </Row>
                {values.length > 0 && (
                  <Facts
                    micro
                    facts={values.map((value) => ({
                      key: value.field,
                      label: value.field,
                      value: value.text,
                    }))}
                  />
                )}
                {flow.description !== "" && <SectionText>{flow.description}</SectionText>}
                {stale.length > 0 && <StaleList entries={stale} />}
              </ListRow>
            );
          })}
        </List>
      )}
    </Section>
  );
}
