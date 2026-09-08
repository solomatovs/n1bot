import { Eraser, GitCompare, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import type { WorkflowApi } from "../../api/client";
import { ApiError } from "../../api/transport";
import { PageUrls } from "../../config";
import { formatStamp } from "../../model/time";
import { Async } from "../../components/Async";
import { useLoadable } from "../../hooks/useLoadable";
import type { CatalogApi } from "../api/client";
import { useObjectCard } from "../hooks/useObjectCard";
import { useCatalog, useCatalogChanges } from "../services";
import { SyncDialog } from "../components/connections/SyncDialog";
import { DiffPanel } from "../components/sources/DiffPanel";
import { ObjectCardPanel } from "../components/sources/ObjectCardPanel";
import { SourceTree } from "../components/sources/SourceTree";
import { SyncBar } from "../components/tasks/TaskBar";
import type { ConnectionView } from "../../model/account";
import type { CatalogChanged } from "../../model/workflow";
import type {
  Access,
  ConnectionVersion,
  ObjectCard,
  ObjectRef,
  SourceDiff,
  Sync,
  SyncedConnection,
  TreeNode,
} from "../model/catalog";
import { readConnectionUrlState, writeConnectionUrlState, type ConnectionUrlState } from "../model/urlState";
import {
  Alert,
  Button,
  Chip,
  Dialog,
  EmptyState,
  Page,
  PageBody,
  PageNotices,
  Pane,
  Scene,
  Select,
  Toolbar,
  Topbar,
  TopbarGroup,
  TopbarHint,
  TopbarLink,
  TopbarSpacer,
  TopbarTitle,
  useToast,
} from "../../ui";

type Loaded = {
  access: Access;
  /** Строка брокера; null — подключение не видно (чужое личное), есть только снимок. */
  view: ConnectionView | null;
  synced: SyncedConnection | null;
  versions: ConnectionVersion[];
  syncs: Sync[];
  syncable: boolean;
};

/** Страница подключения глазами каталога: дерево версии снимка слева,
 * родная карточка объекта справа, выбор версии и разница с предыдущей,
 * синхронизация с ходом, история синхронизаций, «forget versions». */
export function ConnectionPage(): ReactElement {
  const { connectionId } = useParams();
  if (connectionId === undefined) {
    return <EmptyState fill title="connection id is missing" />;
  }

  return <ConnectionView connectionId={connectionId} />;
}

function ConnectionView({ connectionId }: { connectionId: string }): ReactElement {
  const { api, workflowApi } = useCatalog();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const [diff, setDiff] = useState<SourceDiff | null>(null);
  const [dialog, setDialog] = useState<"none" | "sync" | "forget">("none");
  const [reloads, setReloads] = useState(0);
  const [latestSync, setLatestSync] = useState<Sync | null>(null);

  const urlState = useMemo(() => readConnectionUrlState(params), [params]);
  const requestedVersion = urlState.version;
  const showDiff = urlState.showDiff;
  const selected = urlState.ref;

  const [state, reload] = useLoadable(
    useCallback(() => load(api, workflowApi, connectionId), [api, workflowApi, connectionId]),
    { keep: true },
  );

  // каждая загрузка — новая версия страницы: дерево и diff перечитываются
  useEffect(() => {
    if (state.kind !== "ready") {
      return;
    }

    setLatestSync(state.value.syncs[0] ?? null);
    setReloads((count) => count + 1);
  }, [state]);

  useCatalogChanges(
    useCallback(
      (message: CatalogChanged) => {
        if (message.connection_id === connectionId) {
          reload();
          return;
        }

        if (message.sync_id === null) {
          return;
        }

        api
          .sync(message.sync_id)
          .then((sync) => {
            if (sync.connection_id === connectionId) {
              setLatestSync(sync);
            }
          })
          .catch((error: unknown) => {
            toast(ApiError.describe(error), "error");
          });
      },
      [api, connectionId, reload, toast],
    ),
  );

  let latest = 0;
  if (state.kind === "ready") {
    latest = state.value.synced?.latest_version ?? 0;
  }
  const version = resolveVersion(latest, requestedVersion);

  const loadTree = useCallback(
    (path: string[]) => api.connectionTree(connectionId, version, path),
    [api, connectionId, version],
  );

  useEffect(() => {
    if (!showDiff || version < 2) {
      setDiff(null);
      return;
    }

    let cancelled = false;
    api
      .connectionDiff(connectionId, version - 1, version)
      .then((loaded) => {
        if (!cancelled) {
          setDiff(loaded);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          toast(ApiError.describe(error), "error");
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, connectionId, version, showDiff, toast, reloads]);

  const renderConnection = ({ access, view, synced, versions, syncable }: Loaded): ReactElement => {
    const name = view?.name ?? synced?.name ?? connectionId;
    const kind = view?.kind ?? synced?.kind ?? "?";
    const canSync = access.can_edit && view !== null && syncable;
    const setUrlState = (patch: Partial<ConnectionUrlState>): void => {
      setParams((current) => writeConnectionUrlState({ ...readConnectionUrlState(current), ...patch }, current), {
        replace: true,
      });
    };

    const select = (node: TreeNode): void => {
      if (node.ref === null) {
        return;
      }

      setUrlState({ ref: node.ref, showDiff: false });
    };

    return (
      <Page mark="connection-page" data-connection={name} data-version={version} data-can-edit={access.can_edit}>
        <Topbar>
          <TopbarLink to={PageUrls.catalog.home()}>catalog</TopbarLink>
          <TopbarLink to={PageUrls.connections()}>connections</TopbarLink>
          <TopbarTitle>{name}</TopbarTitle>
          <Chip tone="muted">{kind}</Chip>
          {view === null && <Chip tone="muted">not yours</Chip>}
          <Select
            aria-label="snapshot version"
            value={String(version)}
            onChange={(event) => {
              setUrlState({ version: Number(event.target.value), showDiff: false });
            }}
          >
            {versions.length === 0 && <option value="0">no versions</option>}
            {versions.map((item) => (
              <option key={item.version} value={String(item.version)}>
                v{item.version} · {item.objects_total} objects · {formatStamp(item.taken_at)}
              </option>
            ))}
          </Select>
          {version >= 2 && (
            <Button
              size="sm"
              tone={showDiff ? "signal" : "ghost"}
              icon={GitCompare}
              collapsible
              aria-pressed={showDiff}
              onClick={() => {
                setUrlState({ showDiff: !showDiff });
              }}
            >
              diff with v{version - 1}
            </Button>
          )}
          <TopbarGroup>
            {canSync && (
              <Button
                size="sm"
                tone="primary"
                icon={RefreshCw}
                disabled={latestSync?.status === "running"}
                onClick={() => {
                  setDialog("sync");
                }}
                data-testid="connection-sync"
              >
                sync
              </Button>
            )}
            {access.can_edit && versions.length > 0 && (
              <Button
                size="sm"
                tone="ghost"
                icon={Eraser}
                collapsible
                onClick={() => {
                  setDialog("forget");
                }}
                data-testid="forget-versions"
              >
                forget versions
              </Button>
            )}
          </TopbarGroup>
          <TopbarSpacer />
          <TopbarHint>{versions.length === 0 ? "not synced yet" : `${versions.length} version(s)`}</TopbarHint>
        </Topbar>
        <PageNotices>
          {latestSync !== null && (
            <SyncBar
              sync={latestSync}
              canCancel={access.can_edit}
              onCancel={() => {
                api
                  .cancelSync(latestSync.id)
                  .then((sync) => {
                    setLatestSync(sync);
                    toast("sync cancelled", "success");
                  })
                  .catch((error: unknown) => {
                    toast(ApiError.describe(error), "error");
                  });
              }}
            />
          )}
        </PageNotices>
        <PageBody>
          <Pane>
            {versions.length === 0 ? (
              <EmptyState title="no versions yet">
                {canSync ? "run a synchronisation to load the structure" : "nothing has been synced from it"}
              </EmptyState>
            ) : (
              <SourceTree load={loadTree} reloadKey={`${version}:${reloads}`} selected={selected} onSelect={select} />
            )}
          </Pane>
          <Scene panel>
            {diff !== null && showDiff ? (
              <DiffPanel diff={diff} title={`v${version - 1} → v${version}`} />
            ) : (
              <ObjectScene api={api} connectionId={connectionId} version={version} selected={selected} />
            )}
          </Scene>
        </PageBody>
        {dialog === "sync" && (
          <SyncDialog
            connectionName={name}
            onStart={(scope) => {
              api
                .startSync(connectionId, scope)
                .then((sync) => {
                  setDialog("none");
                  setLatestSync(sync);
                  toast("sync started", "success");
                })
                .catch((error: unknown) => {
                  toast(ApiError.describe(error), "error");
                });
            }}
            onClose={() => {
              setDialog("none");
            }}
          />
        )}
        {dialog === "forget" && (
          <Dialog
            title="forget the catalog versions"
            mark="forget-versions"
            onClose={() => {
              setDialog("none");
            }}
          >
            <Alert tone="info">
              All {versions.length} snapshot version(s) of “{name}” will be removed from the catalog. The connection
              itself stays. Refused while nodes of a process point at its objects.
            </Alert>
            <Toolbar>
              <Button
                tone="danger"
                onClick={() => {
                  api
                    .forgetVersions(connectionId)
                    .then((count) => {
                      setDialog("none");
                      toast(`${count} version(s) forgotten`, "success");
                      reload();
                    })
                    .catch((error: unknown) => {
                      toast(ApiError.describe(error), "error");
                    });
                }}
                data-testid="forget-versions-confirm"
              >
                forget the versions
              </Button>
              <Button
                tone="ghost"
                onClick={() => {
                  setDialog("none");
                }}
              >
                cancel
              </Button>
            </Toolbar>
          </Dialog>
        )}
      </Page>
    );
  };

  return (
    <Async
      state={state}
      fill
      title={{ loading: "loading the connection", failed: "the connection is not available" }}
      render={renderConnection}
    />
  );
}

type SceneProps = {
  api: CatalogApi;
  connectionId: string;
  version: number;
  selected: ObjectRef | undefined;
};

/** Сцена страницы: подсказка без выбора, иначе карточка выбранного объекта. */
function ObjectScene({ api, connectionId, version, selected }: SceneProps): ReactElement {
  if (selected === undefined || version === 0) {
    return <EmptyState fill title="pick an object in the tree" />;
  }

  return <ObjectCardScene api={api} connectionId={connectionId} version={version} object={selected} />;
}

type CardSceneProps = Omit<SceneProps, "selected"> & { object: ObjectRef };

function ObjectCardScene({ api, connectionId, version, object }: CardSceneProps): ReactElement {
  const card = useObjectCard(api, connectionId, version, object);
  const renderCard = (loaded: ObjectCard): ReactElement => <ObjectCardPanel card={loaded} />;

  return (
    <Async
      state={card}
      fill
      title={{ loading: "loading the object", failed: "the object is not available" }}
      render={renderCard}
    />
  );
}

function resolveVersion(latest: number, requested: number): number {
  if (requested < 0 || requested > latest) {
    return latest;
  }

  return requested;
}

async function load(api: CatalogApi, workflowApi: WorkflowApi, connectionId: string): Promise<Loaded> {
  const access = await api.access();
  const kinds = await api.sourceKinds();
  const views = await workflowApi.connections();
  const view = views.find((item) => item.id === connectionId) ?? null;
  const synced = (await api.synced()).find((item) => item.connection_id === connectionId) ?? null;
  if (view === null && synced === null) {
    throw new Error(`connection ${connectionId} is neither visible to you nor synced into the catalog`);
  }

  const versions = synced === null ? [] : await api.connectionVersions(connectionId);
  const syncs = await api.connectionSyncs(connectionId);
  const syncable = view !== null && kinds.includes(view.kind);

  return { access, view, synced, versions, syncs, syncable };
}
