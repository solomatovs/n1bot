import { Eye, EyeOff, Pencil, Plus, Trash2 } from "lucide-react";
import { useMemo, useState, type ReactElement } from "react";

import type { CatalogApi } from "../api/client";
import type { Catalog, ObjectRef, ProcessNode } from "../model/catalog";
import type { EditActions } from "../model/editing";
import type { PaneTab } from "../model/urlState";
import { Button, Eyebrow, IconButton, Input, Segmented } from "../ui";
import { SourcesPane } from "./SourcesPane";

type Props = {
  api: CatalogApi;
  catalog: Catalog;
  nodes: ProcessNode[];
  tab: PaneTab;
  onTab: (tab: PaneTab) => void;
  activeId: string | undefined;
  selectedObject: ObjectRef | undefined;
  hidden: ReadonlySet<string>;
  showDiff: boolean;
  editing: EditActions | undefined;
  onActivate: (nodeId: string) => void;
  onSelectObject: (ref: ObjectRef) => void;
  onToggleHidden: (nodeId: string) => void;
};

const TABS: { value: PaneTab; label: string }[] = [
  { value: "process", label: "process" },
  { value: "sources", label: "sources" },
];

/** Левая панель: вкладка процесса — узлы по слоям с поиском, выбором и глазом,
 * который прячет узел на холсте; вкладка источников — деревья источников, из
 * которых на черновике берутся узлы. */
export function LeftPane({
  api,
  catalog,
  nodes,
  tab,
  onTab,
  activeId,
  selectedObject,
  hidden,
  showDiff,
  editing,
  onActivate,
  onSelectObject,
  onToggleHidden,
}: Props): ReactElement {
  return (
    <div className="pane" data-testid="left-pane" data-tab={tab}>
      <div className="pane__tabs">
        <Segmented options={TABS} value={tab} onChange={onTab} label="left pane tab" />
      </div>
      {tab === "process" ? (
        <ProcessList
          catalog={catalog}
          nodes={nodes}
          activeId={activeId}
          hidden={hidden}
          showDiff={showDiff}
          editing={editing}
          onActivate={onActivate}
          onToggleHidden={onToggleHidden}
        />
      ) : (
        <SourcesPane
          api={api}
          pins={catalog.context.pins}
          selected={selectedObject}
          onSelect={onSelectObject}
          draggable={editing !== undefined}
        />
      )}
    </div>
  );
}

type ListProps = {
  catalog: Catalog;
  nodes: ProcessNode[];
  activeId: string | undefined;
  hidden: ReadonlySet<string>;
  showDiff: boolean;
  editing: EditActions | undefined;
  onActivate: (nodeId: string) => void;
  onToggleHidden: (nodeId: string) => void;
};

function ProcessList({ catalog, nodes, activeId, hidden, showDiff, editing, onActivate, onToggleHidden }: ListProps): ReactElement {
  const [query, setQuery] = useState("");

  // в черновике пустые слои видны: в них кладут узлы
  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return catalog.layers
      .map((layer) => ({
        layer,
        nodes: nodes.filter((node) => {
          if (node.layer_id !== layer.id) {
            return false;
          }

          if (needle === "") {
            return true;
          }

          return catalog.label(node.id).toLowerCase().includes(needle) || node.ref.path.join("/").toLowerCase().includes(needle);
        }),
      }))
      .filter((group) => group.nodes.length > 0 || (editing !== undefined && needle === ""));
  }, [catalog, nodes, query, editing]);

  return (
    <>
      <div className="pane__search">
        <Input
          type="search"
          mono
          placeholder="find a node"
          aria-label="find a node"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
          }}
        />
      </div>
      <div className="pane__scroll">
        {groups.map((group) => (
          <section key={group.layer.id} className="pane__group" data-layer={group.layer.name}>
            <div className="pane__group-head">
              <Eyebrow>{group.layer.name}</Eyebrow>
              {editing !== undefined && (
                <span className="pane__group-actions">
                  <IconButton
                    className="pane__eye"
                    aria-label={`rename layer ${group.layer.name}`}
                    onClick={() => {
                      editing.renameLayer(group.layer);
                    }}
                  >
                    <Pencil size={12} />
                  </IconButton>
                  {group.nodes.length === 0 && (
                    <IconButton
                      className="pane__eye"
                      aria-label={`remove layer ${group.layer.name}`}
                      onClick={() => {
                        editing.removeLayer(group.layer);
                      }}
                    >
                      <Trash2 size={12} />
                    </IconButton>
                  )}
                </span>
              )}
            </div>
            <ul className="pane__list">
              {group.nodes.map((node) => {
                const status = showDiff ? catalog.statusOf("node", node.id) : "unchanged";
                const label = catalog.label(node.id);
                return (
                  <li
                    key={node.id}
                    className="pane__item"
                    data-active={node.id === activeId}
                    data-hidden={hidden.has(node.id)}
                    data-status={status}
                    data-stale={catalog.staleOf("node", node.id).length > 0}
                    data-node={node.ref.path.join("/")}
                    data-testid="pane-item"
                  >
                    <button
                      type="button"
                      className="pane__name"
                      onClick={() => {
                        onActivate(node.id);
                      }}
                    >
                      {label}
                    </button>
                    <IconButton
                      className="pane__eye"
                      aria-label={hidden.has(node.id) ? `show ${label}` : `hide ${label}`}
                      aria-pressed={hidden.has(node.id)}
                      onClick={() => {
                        onToggleHidden(node.id);
                      }}
                    >
                      {hidden.has(node.id) ? <EyeOff size={14} /> : <Eye size={14} />}
                    </IconButton>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
        {groups.length === 0 && <p className="pane__empty">nothing matches</p>}
        {editing !== undefined && (
          <div className="pane__footer">
            <Button size="tiny" icon={Plus} onClick={editing.addLayer}>
              layer
            </Button>
          </div>
        )}
      </div>
    </>
  );
}
