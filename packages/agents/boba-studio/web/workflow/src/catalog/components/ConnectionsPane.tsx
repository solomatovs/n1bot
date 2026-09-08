import { ChevronDown, ChevronRight, GitCompare, Plus } from "lucide-react";
import { useCallback, useEffect, useState, type ReactElement } from "react";
import { Link } from "react-router-dom";

import { useLoadable } from "../../hooks/useLoadable";
import { PageUrls } from "../../config";
import type { CatalogApi } from "../api/client";
import type { CatalogChanged } from "../../model/workflow";
import type { ConnectionVersion, ObjectRef, SyncedConnection, TreeNode } from "../model/catalog";
import { Button, Chip, IconButton, Note, Select, useToast } from "../../ui";
import { useCatalogChanges } from "../services";
import { PaneGroup } from "./LeftPane";
import { SourceTree } from "./sources/SourceTree";

type Props = {
  api: CatalogApi;
  /** Версии снимков, к которым привязан процесс: с них дерево начинается;
   * без привязки — последняя. Показ другой версии — только просмотр,
   * процесс работает над привязанной. */
  pins: Record<string, number>;
  selected: ObjectRef | undefined;
  onSelect: (ref: ObjectRef) => void;
  /** На черновике объекты тащатся на холст. */
  draggable: boolean;
  /** Плюс у списка подключений: форма нового подключения; без права на
   * правки — нет. */
  onAdd: (() => void) | undefined;
  /** Черновик нового процесса из подключения; без права на правки — нет. */
  onProcessFrom: ((name: string) => void) | undefined;
};

/** Подключения со снимком в левой панели: закреплённая полоса со ссылкой на
 * страницу подключений сверху, ниже список с плюсом в заголовке, где каждое
 * подключение раскрывается в дерево той версии, к которой привязан процесс;
 * объект выбирается в панель деталей и на черновике тащится на холст. */
export function ConnectionsPane({
  api,
  pins,
  selected,
  onSelect,
  draggable,
  onAdd,
  onProcessFrom,
}: Props): ReactElement {
  const [state, reload] = useLoadable(
    useCallback(() => api.synced(), [api]),
    { keep: true },
  );

  useCatalogChanges(
    useCallback(
      (message: CatalogChanged) => {
        if (message.connection_id !== null) {
          reload();
        }
      },
      [reload],
    ),
  );

  let title = "connections";
  if (state.kind === "ready") {
    title = `connections · ${state.value.length}`;
  }

  return (
    <>
      <div className="pane__bar pane__actions" data-testid="connections-actions">
        <Link to={PageUrls.connections()} data-testid="connections-link">
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
            {state.kind === "loading" && (
              <Note pad mark="pane-empty">
                loading connections…
              </Note>
            )}
            {state.kind === "error" && (
              <Note pad tone="error" mark="pane-empty">
                {state.message}
              </Note>
            )}
            {state.kind === "ready" && state.value.length === 0 && (
              <Note pad mark="pane-empty">
                no synced connections yet: add one and sync it
              </Note>
            )}
            {state.kind === "ready" &&
              state.value.map((connection) => (
                <ConnectionBranch
                  key={connection.connection_id}
                  api={api}
                  connection={connection}
                  pinned={pins[connection.connection_id] ?? -1}
                  selected={selected}
                  onSelect={onSelect}
                  draggable={draggable}
                  onProcessFrom={onProcessFrom}
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
  /** Привязанная версия; отрицательная — последняя. */
  pinned: number;
  selected: ObjectRef | undefined;
  onSelect: (ref: ObjectRef) => void;
  draggable: boolean;
  onProcessFrom: ((name: string) => void) | undefined;
};

/** Подключение в панели: раскрывается в дерево выбранной версии снимка.
 * Версия по умолчанию — привязанная у процесса (у входа — последняя), список
 * версий подгружается при раскрытии; выбор другой версии — просмотр её
 * объектов, привязку процесса он не меняет (её поднимает upgrade). Рядом —
 * сравнение выбранной версии с предыдущей и черновик нового процесса из
 * этого подключения. */
function ConnectionBranch({
  api,
  connection,
  pinned,
  selected,
  onSelect,
  draggable,
  onProcessFrom,
}: BranchProps): ReactElement {
  const [open, setOpen] = useState(false);
  const [chosen, setChosen] = useState<number | undefined>(undefined);
  const toast = useToast();
  const id = connection.connection_id;
  let version = connection.latest_version;
  if (pinned >= 0) {
    version = pinned;
  }
  if (chosen !== undefined) {
    version = chosen;
  }

  const load = useCallback((path: string[]) => api.connectionTree(id, version, path), [api, id, version]);
  const own = selected?.connection_id === id ? selected : undefined;

  // список версий подгружается при раскрытии и перечитывается с новым снимком
  const loadVersions = useCallback((): Promise<ConnectionVersion[]> => {
    if (!open) {
      return Promise.resolve([]);
    }

    return api.connectionVersions(id);
  }, [api, id, open]);
  const [versions] = useLoadable(loadVersions, { key: String(connection.latest_version) });

  useEffect(() => {
    if (versions.kind === "error") {
      toast(versions.message, "error");
    }
  }, [versions, toast]);

  const select = (node: TreeNode): void => {
    if (node.ref !== null) {
      onSelect(node.ref);
    }
  };

  let versionMark = "";
  if (version === pinned) {
    versionMark = " · pinned";
  }
  if (version === connection.latest_version) {
    versionMark = " · latest";
  }

  const options: ReactElement[] = [];
  if (versions.kind === "ready") {
    for (const item of versions.value) {
      let mark = "";
      if (item.version === pinned) {
        mark = " · pinned";
      }
      options.push(
        <option key={item.version} value={String(item.version)}>
          v{item.version}
          {mark}
        </option>,
      );
    }
  }

  let versionControl = (
    <Chip tone="muted" mark="branch-version">
      v{version}
      {versionMark}
    </Chip>
  );
  if (open && options.length > 0) {
    versionControl = (
      <Select
        narrow
        aria-label={`snapshot version of ${connection.name}`}
        value={String(version)}
        onChange={(event) => {
          setChosen(Number(event.target.value));
        }}
      >
        {options}
      </Select>
    );
  }

  return (
    <PaneGroup
      title={connection.name}
      name
      nested
      mark="connection-branch"
      data={{ "data-connection": connection.name, "data-open": open, "data-version": String(version) }}
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
          {versionControl}
          {version >= 2 && (
            <Link to={PageUrls.catalog.connection(id, { v: version, mode: "diff" })} data-testid="branch-diff">
              <IconButton size="sm" ghost aria-label={`diff of ${connection.name} v${version} with v${version - 1}`}>
                <GitCompare size={12} />
              </IconButton>
            </Link>
          )}
          {onProcessFrom !== undefined && (
            <IconButton
              size="sm"
              ghost
              aria-label={`new process from ${connection.name}`}
              onClick={() => {
                onProcessFrom(connection.name);
              }}
              data-testid="process-from"
            >
              <Plus size={12} />
            </IconButton>
          )}
        </>
      }
    >
      {open && (
        <SourceTree load={load} reloadKey={`${id}:${version}`} selected={own} onSelect={select} draggable={draggable} />
      )}
    </PaneGroup>
  );
}

