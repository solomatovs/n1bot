import { Eye, EyeOff, Pencil, Plus, Trash2 } from "lucide-react";
import { useMemo, useState, type ReactElement } from "react";

import type { CatalogApi } from "../api/client";
import type { Catalog, ObjectRef, ProcessNode } from "../model/catalog";
import type { EditActions } from "../model/editing";
import type { PaneTab } from "../model/urlState";
import { Button, IconButton, List, ListAside, ListName, ListRow, Note, Search, Segmented, Toolbar } from "../ui";
import "./pane.css";
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
      <div className="pane__bar">
        <Segmented options={TABS} value={tab} onChange={onTab} label="left pane tab" fill />
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

function ProcessList({
  catalog,
  nodes,
  activeId,
  hidden,
  showDiff,
  editing,
  onActivate,
  onToggleHidden,
}: ListProps): ReactElement {
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

          return (
            catalog.label(node.id).toLowerCase().includes(needle) ||
            node.ref.path.join("/").toLowerCase().includes(needle)
          );
        }),
      }))
      .filter((group) => group.nodes.length > 0 || (editing !== undefined && needle === ""));
  }, [catalog, nodes, query, editing]);

  return (
    <>
      <div className="pane__bar">
        <Search value={query} onChange={setQuery} label="find a node" placeholder="find a node" />
      </div>
      <div className="pane__scroll">
        {groups.map((group) => (
          <PaneGroup
            key={group.layer.id}
            title={group.layer.name}
            data={{ "data-layer": group.layer.name }}
            actions={
              editing !== undefined && (
                <>
                  <IconButton
                    size="sm"
                    ghost
                    aria-label={`rename layer ${group.layer.name}`}
                    onClick={() => {
                      editing.renameLayer(group.layer);
                    }}
                  >
                    <Pencil size={12} />
                  </IconButton>
                  {group.nodes.length === 0 && (
                    <IconButton
                      size="sm"
                      ghost
                      aria-label={`remove layer ${group.layer.name}`}
                      onClick={() => {
                        editing.removeLayer(group.layer);
                      }}
                    >
                      <Trash2 size={12} />
                    </IconButton>
                  )}
                </>
              )
            }
          >
            <List>
              {group.nodes.map((node) => {
                const status = showDiff ? catalog.statusOf("node", node.id) : "unchanged";
                const label = catalog.label(node.id);
                return (
                  <ListRow
                    key={node.id}
                    active={node.id === activeId}
                    hidden={hidden.has(node.id)}
                    status={status}
                    stale={catalog.staleOf("node", node.id).length > 0}
                    data-node={node.ref.path.join("/")}
                    mark="pane-item"
                  >
                    <ListName
                      onClick={() => {
                        onActivate(node.id);
                      }}
                    >
                      {label}
                    </ListName>
                    <ListAside>
                      <IconButton
                        size="sm"
                        ghost
                        aria-label={hidden.has(node.id) ? `show ${label}` : `hide ${label}`}
                        aria-pressed={hidden.has(node.id)}
                        onClick={() => {
                          onToggleHidden(node.id);
                        }}
                      >
                        {hidden.has(node.id) ? <EyeOff size={14} /> : <Eye size={14} />}
                      </IconButton>
                    </ListAside>
                  </ListRow>
                );
              })}
            </List>
          </PaneGroup>
        ))}
        {groups.length === 0 && (
          <Note pad mark="pane-empty">
            nothing matches
          </Note>
        )}
        {editing !== undefined && (
          <Toolbar pad>
            <Button size="sm" icon={Plus} onClick={editing.addLayer}>
              layer
            </Button>
          </Toolbar>
        )}
      </div>
    </>
  );
}

type GroupProps = {
  title: ReactElement | string;
  /** Заголовок — имя (моно, без капители), не подпись группы. */
  name?: boolean;
  actions?: ReactElement | false | undefined;
  lead?: ReactElement | undefined;
  data?: Record<string, string | boolean | undefined>;
  mark?: string | undefined;
  children?: ReactElement | false | undefined;
};

/** Группа панели: заголовок капителью (или именем) с действиями и список
 * под ним. Единственное место, где существуют классы `pane*`. */
export function PaneGroup({ title, name = false, actions, lead, data, mark, children }: GroupProps): ReactElement {
  const titleClass = name ? "pane__group-title pane__group-title--name" : "pane__group-title";

  return (
    <section className="pane__group" data-testid={mark} {...data}>
      <div className="pane__group-head">
        {lead}
        <span className={titleClass}>{title}</span>
        {actions !== undefined && actions !== false && <span className="pane__group-actions">{actions}</span>}
      </div>
      {children}
    </section>
  );
}
