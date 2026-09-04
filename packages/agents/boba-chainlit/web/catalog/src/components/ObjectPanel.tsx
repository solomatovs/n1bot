import { Crosshair, Plus, X } from "lucide-react";
import { useEffect, useState, type ReactElement } from "react";

import { ApiError, type CatalogApi } from "../api/client";
import { renderRef, type Catalog, type ObjectCard, type ObjectRef, type ProcessNode } from "../model/catalog";
import type { EditActions } from "../model/editing";
import { Alert, Button, Chip, EmptyState, Eyebrow, IconButton, Select } from "../ui";
import { ObjectCardPanel } from "./sources/ObjectCardPanel";

type Props = {
  api: CatalogApi;
  catalog: Catalog;
  object: ObjectRef;
  editing: EditActions | undefined;
  /** Узел, который ждёт перенацеливания на этот объект. */
  retargetFor: ProcessNode | undefined;
  onOpenNode: (nodeId: string) => void;
  onClose: () => void;
};

type CardState = { status: "loading" } | { status: "failed"; message: string } | { status: "card"; card: ObjectCard };

/** Панель объекта из дерева источника: родная карточка привязанной версии и
 * действия процесса — поставить в слой, открыть узел, перенацелить узел. */
export function ObjectPanel({ api, catalog, object, editing, retargetFor, onOpenNode, onClose }: Props): ReactElement {
  const ref = object;
  const [state, setState] = useState<CardState>({ status: "loading" });
  const [layerId, setLayerId] = useState(catalog.layers[0]?.id ?? "");
  const version = catalog.context.pins[ref.source_id] ?? -1;
  const address = renderRef(ref);
  const existing = catalog.nodeOf(ref);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    api
      .sourceObject(ref.source_id, version, ref.kind, ref.path)
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
  }, [api, ref, version]);

  const chosenLayer = layerId === "" ? catalog.layers[0]?.id : layerId;

  return (
    <div className="detail" data-testid="object-panel" data-object={address} data-in-process={existing !== undefined}>
      <header className="detail__head">
        <div className="detail__title">
          <Eyebrow>source object</Eyebrow>
          <h2 className="detail__name mono">{ref.path.at(-1) ?? address}</h2>
        </div>
        <Chip tone="muted">{ref.kind}</Chip>
        <IconButton aria-label="close details" onClick={onClose}>
          <X size={16} />
        </IconButton>
      </header>

      <section className="detail__section" data-testid="object-actions">
        <dl className="detail__facts">
          <dt>address</dt>
          <dd className="mono">{address}</dd>
          <dt>version</dt>
          <dd>{version < 0 ? "latest" : `v${version}`}</dd>
        </dl>
        {existing !== undefined && (
          <div className="form__actions">
            <Chip tone="draft">in layer {catalog.layer(existing.layer_id)?.name ?? "?"}</Chip>
            <Button
              size="tiny"
              onClick={() => {
                onOpenNode(existing.id);
              }}
            >
              open node
            </Button>
          </div>
        )}
        {editing !== undefined && retargetFor !== undefined && (
          <div className="form__actions">
            <Button
              size="tiny"
              tone="signal"
              icon={Crosshair}
              disabled={existing !== undefined}
              onClick={() => {
                editing.retargetNode(retargetFor, ref);
              }}
            >
              retarget {catalog.label(retargetFor.id)} here
            </Button>
          </div>
        )}
        {editing !== undefined && existing === undefined && retargetFor === undefined && (
          <div className="form__actions object-panel__add">
            <Select
              aria-label="layer for the new node"
              value={chosenLayer ?? ""}
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
            <Button
              size="tiny"
              tone="primary"
              icon={Plus}
              disabled={chosenLayer === undefined}
              onClick={() => {
                if (chosenLayer !== undefined) {
                  editing.addNode(chosenLayer, ref);
                }
              }}
            >
              add to layer
            </Button>
          </div>
        )}
        {editing !== undefined && catalog.layers.length === 0 && (
          <Alert tone="info" mark="no-layers">
            Add a layer first: nodes live in layers.
          </Alert>
        )}
      </section>

      {state.status === "loading" && <p className="detail__empty">loading the object…</p>}
      {state.status === "failed" && (
        <EmptyState title="the object is not available">{state.message}</EmptyState>
      )}
      {state.status === "card" && (
        <section className="detail__section detail__source-card" data-testid="object-card-section">
          <ObjectCardPanel card={state.card} />
        </section>
      )}
    </div>
  );
}
