import { ChevronDown, ChevronRight, Plus } from "lucide-react";
import { useCallback, useEffect, useState, type ReactElement } from "react";
import { Link } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import type { ObjectRef, SyncedConnection, TreeNode } from "../model/catalog";
import { Button, Chip, IconButton, Note } from "../ui";
import { PaneGroup } from "./LeftPane";
import { SourceTree } from "./sources/SourceTree";

type Props = {
  api: CatalogApi;
  /** Версии снимков, к которым привязан процесс; без привязки — последняя. */
  pins: Record<string, number>;
  selected: ObjectRef | undefined;
  onSelect: (ref: ObjectRef) => void;
  /** На черновике объекты тащатся на холст. */
  draggable: boolean;
  /** Плюс у списка подключений: форма нового подключения; без права на
   * правки — нет. */
  onAdd: (() => void) | undefined;
};

type Loaded =
  | { status: "loading" }
  | { status: "failed"; message: string }
  | { status: "ready"; connections: SyncedConnection[] };

/** Подключения со снимком в левой панели: закреплённая полоса со ссылкой на
 * страницу подключений сверху, ниже список с плюсом в заголовке, где каждое
 * подключение раскрывается в дерево той версии, к которой привязан процесс;
 * объект выбирается в панель деталей и на черновике тащится на холст. */
export function ConnectionsPane({ api, pins, selected, onSelect, draggable, onAdd }: Props): ReactElement {
  const [state, setState] = useState<Loaded>({ status: "loading" });

  const reload = useCallback(() => {
    let cancelled = false;
    api
      .synced()
      .then((connections) => {
        if (!cancelled) {
          setState({ status: "ready", connections });
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
  }, [api]);

  useEffect(() => reload(), [reload]);

  useEffect(() => {
    return api.events((message) => {
      if (message.connection_id !== null) {
        reload();
      }
    });
  }, [api, reload]);

  let title = "connections";
  if (state.status === "ready") {
    title = `connections · ${state.connections.length}`;
  }

  return (
    <>
      <div className="pane__bar pane__actions" data-testid="connections-actions">
        <Link to="/connections" data-testid="connections-link">
          <Button size="sm" tone="ghost">
            all connections
          </Button>
        </Link>
      </div>
      <div className="pane__scroll" data-testid="connections-pane">
        <PaneGroup
          title={title}
          mark="connections-group"
          actions={
            onAdd !== undefined && (
              <IconButton size="sm" ghost aria-label="new connection" onClick={onAdd} data-testid="add-connection">
                <Plus size={12} />
              </IconButton>
            )
          }
        >
          <>
            {state.status === "loading" && (
              <Note pad mark="pane-empty">
                loading connections…
              </Note>
            )}
            {state.status === "failed" && (
              <Note pad tone="error" mark="pane-empty">
                {state.message}
              </Note>
            )}
            {state.status === "ready" && state.connections.length === 0 && (
              <Note pad mark="pane-empty">
                no synced connections yet: add one and sync it
              </Note>
            )}
            {state.status === "ready" &&
              state.connections.map((connection) => (
                <ConnectionBranch
                  key={connection.connection_id}
                  api={api}
                  connection={connection}
                  version={pins[connection.connection_id] ?? -1}
                  selected={selected}
                  onSelect={onSelect}
                  draggable={draggable}
                />
              ))}
          </>
        </PaneGroup>
      </div>
    </>
  );
}

type BranchProps = {
  api: CatalogApi;
  connection: SyncedConnection;
  version: number;
  selected: ObjectRef | undefined;
  onSelect: (ref: ObjectRef) => void;
  draggable: boolean;
};

function ConnectionBranch({ api, connection, version, selected, onSelect, draggable }: BranchProps): ReactElement {
  const [open, setOpen] = useState(false);
  const id = connection.connection_id;
  const load = useCallback((path: string[]) => api.connectionTree(id, version, path), [api, id, version]);
  const pinned = version < 0 ? `v${connection.latest_version}` : `v${version}`;
  const own = selected?.connection_id === id ? selected : undefined;

  const select = (node: TreeNode): void => {
    if (node.ref !== null) {
      onSelect(node.ref);
    }
  };

  return (
    <PaneGroup
      title={connection.name}
      name
      nested
      mark="connection-branch"
      data={{ "data-connection": connection.name, "data-open": open }}
      lead={
        <IconButton
          size="sm"
          ghost
          aria-label={`${open ? "collapse" : "expand"} connection ${connection.name}`}
          onClick={() => {
            setOpen((current) => !current);
          }}
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </IconButton>
      }
      actions={
        <>
          <Chip tone="muted">{connection.kind}</Chip>
          <Chip tone="muted">{pinned}</Chip>
        </>
      }
    >
      {open && (
        <SourceTree load={load} reloadKey={`${id}:${version}`} selected={own} onSelect={select} draggable={draggable} />
      )}
    </PaneGroup>
  );
}
