import { Crosshair, X } from "lucide-react";
import type { ReactElement } from "react";

import type { CatalogApi } from "../api/client";
import { useObjectCard } from "../hooks/useObjectCard";
import { renderRef, type Catalog, type ObjectRef, type ProcessNode } from "../model/catalog";
import type { EditActions } from "../model/editing";
import { Button, Chip, EmptyState, Facts, IconButton, Note, Panel, PanelHead, Section, Toolbar } from "../../ui";
import { ObjectCardPanel } from "./sources/ObjectCardPanel";

type Props = {
  api: CatalogApi;
  catalog: Catalog;
  object: ObjectRef;
  editing: EditActions | undefined;
  /** Узел, который ждёт перенацеливания на этот объект. */
  retargetFor: ProcessNode | undefined;
  onOpenNode: (nodeId: string) => void;
  /** Другой объект снимка в этой же панели: таблица за внешним ключом. */
  onOpenObject: (ref: ObjectRef) => void;
  onClose: () => void;
};

/** Панель объекта из дерева подключения: родная карточка привязанной версии
 * и действия процесса — поставить в слой, открыть узел, перенацелить узел. */
export function ObjectPanel({
  api,
  catalog,
  object,
  editing,
  retargetFor,
  onOpenNode,
  onOpenObject,
  onClose,
}: Props): ReactElement {
  const ref = object;
  const version = catalog.context.pins[ref.connection_id] ?? -1;
  const address = renderRef(ref);
  const existing = catalog.nodeOf(ref);
  const state = useObjectCard(api, ref.connection_id, version, ref);

  return (
    <div data-testid="object-panel" data-object={address} data-in-process={existing !== undefined}>
      <Panel>
        <PanelHead
          eyebrow="database object"
          name={ref.path.at(-1) ?? address}
          mono
          actions={
            <>
              <Chip tone="muted">{ref.kind}</Chip>
              <IconButton size="sm" ghost aria-label="close details" onClick={onClose}>
                <X size={14} />
              </IconButton>
            </>
          }
        />

        <Section mark="object-actions">
          <Facts
            facts={[
              {
                key: "address",
                label: "address",
                value: <span className="mono">{address}</span>,
              },
              {
                key: "version",
                label: "version",
                value: version < 0 ? "latest" : `v${version}`,
              },
            ]}
          />
          {existing !== undefined && (
            <Toolbar>
              <Chip tone="draft">on the canvas{existing.group_id === null ? "" : ` · ${catalog.group(existing.group_id)?.name ?? "?"}`}</Chip>
              <Button
                size="sm"
                onClick={() => {
                  onOpenNode(existing.id);
                }}
              >
                open node
              </Button>
            </Toolbar>
          )}
          {editing !== undefined && retargetFor !== undefined && (
            <Toolbar>
              <Button
                size="sm"
                tone="signal"
                icon={Crosshair}
                disabled={existing !== undefined}
                onClick={() => {
                  editing.retargetNode(retargetFor, ref);
                }}
              >
                retarget {catalog.label(retargetFor.id)} here
              </Button>
            </Toolbar>
          )}
        </Section>

        {state.kind === "loading" && (
          <Section>
            <Note mark="detail-empty">loading the object…</Note>
          </Section>
        )}
        {state.kind === "error" && <EmptyState title="the object is not available">{state.message}</EmptyState>}
        {state.kind === "ready" && (
          <Section mark="object-card-section">
            <ObjectCardPanel card={state.value} flat onOpenObject={onOpenObject} />
          </Section>
        )}
      </Panel>
    </div>
  );
}
