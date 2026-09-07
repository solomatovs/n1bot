import { Eraser, GitCompare, RefreshCw, XCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import { SyncDialog } from "../components/connections/SyncDialog";
import { DiffPanel } from "../components/sources/DiffPanel";
import { ObjectCardPanel } from "../components/sources/ObjectCardPanel";
import { SourceTree } from "../components/sources/SourceTree";
import type {
  Access,
  ConnectionVersion,
  ConnectionView,
  ObjectCard,
  SourceDiff,
  Sync,
  SyncedConnection,
  TreeNode,
} from "../model/catalog";
import { RefParam } from "../model/refParam";
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
} from "../ui";

type Loaded = {
  access: Access;
  /** Строка брокера; null — подключение не видно (чужое личное), есть только снимок. */
  view: ConnectionView | null;
  synced: SyncedConnection | null;
  versions: ConnectionVersion[];
  syncs: Sync[];
  syncable: boolean;
};
type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; loaded: Loaded };
type Panel =
  | { status: "empty" }
  | { status: "loading" }
  | { status: "failed"; message: string }
  | { status: "card"; card: ObjectCard };

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
  const { api } = useServices();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [panel, setPanel] = useState<Panel>({ status: "empty" });
  const [diff, setDiff] = useState<SourceDiff | null>(null);
  const [dialog, setDialog] = useState<"none" | "sync" | "forget">("none");
  const [reloads, setReloads] = useState(0);
  const [latestSync, setLatestSync] = useState<Sync | null>(null);

  const requestedVersion = Number(params.get("v") ?? "-1");
  const showDiff = params.get("mode") === "diff";
  const selected = useMemo(() => RefParam.parse(params.get("ref")), [params]);

  const reload = useCallback(() => {
    let cancelled = false;
    load(api, connectionId)
      .then((loaded) => {
        if (!cancelled) {
          setState({ status: "ready", loaded });
          setLatestSync(loaded.syncs[0] ?? null);
          setReloads((count) => count + 1);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({ status: "failed", message: describe(error) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, connectionId]);

  useEffect(() => reload(), [reload]);

  useEffect(() => {
    return api.events((message) => {
      if (message.connection_id === connectionId) {
        reload();
        return;
      }

      if (message.sync_id !== null) {
        const syncId = message.sync_id;
        api
          .sync(syncId)
          .then((sync) => {
            if (sync.connection_id === connectionId) {
              setLatestSync(sync);
            }
          })
          .catch((error: unknown) => {
            toast(describe(error), "error");
          });
      }
    });
  }, [api, connectionId, reload, toast]);

  const latest = state.status === "ready" ? (state.loaded.synced?.latest_version ?? 0) : 0;
  const version = resolveVersion(latest, requestedVersion);

  const loadTree = useCallback(
    (path: string[]) => api.connectionTree(connectionId, version, path),
    [api, connectionId, version],
  );

  useEffect(() => {
    if (selected === undefined || version === 0) {
      setPanel({ status: "empty" });
      return;
    }

    let cancelled = false;
    setPanel({ status: "loading" });
    api
      .connectionObject(connectionId, version, selected.kind, selected.path)
      .then((card) => {
        if (!cancelled) {
          setPanel({ status: "card", card });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setPanel({ status: "failed", message: describe(error) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, connectionId, version, selected, reloads]);

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
          toast(describe(error), "error");
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, connectionId, version, showDiff, toast, reloads]);

  if (state.status === "loading") {
    return <EmptyState fill title="loading the connection" />;
  }

  if (state.status === "failed") {
    return (
      <EmptyState fill title="the connection is not available">
        {state.message}
      </EmptyState>
    );
  }

  const { access, view, synced, versions, syncable } = state.loaded;
  const name = view?.name ?? synced?.name ?? connectionId;
  const kind = view?.kind ?? synced?.kind ?? "?";
  const canSync = access.can_edit && view !== null && syncable;
  const setParam = (patch: Record<string, string | undefined>): void => {
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        for (const [key, value] of Object.entries(patch)) {
          if (value === undefined) {
            next.delete(key);
          } else {
            next.set(key, value);
          }
        }
        return next;
      },
      { replace: true },
    );
  };

  const select = (node: TreeNode): void => {
    if (node.ref === null) {
      return;
    }

    setParam({ ref: RefParam.render(node.ref), mode: undefined });
  };

  return (
    <Page mark="connection-page" data-connection={name} data-version={version} data-can-edit={access.can_edit}>
      <Topbar>
        <TopbarLink to="/">catalog</TopbarLink>
        <TopbarLink to="/connections">connections</TopbarLink>
        <TopbarTitle>{name}</TopbarTitle>
        <Chip tone="muted">{kind}</Chip>
        {view === null && <Chip tone="muted">not yours</Chip>}
        <Select
          aria-label="snapshot version"
          value={String(version)}
          onChange={(event) => {
            setParam({ v: event.target.value, mode: undefined });
          }}
        >
          {versions.length === 0 && <option value="0">no versions</option>}
          {versions.map((item) => (
            <option key={item.version} value={String(item.version)}>
              v{item.version} · {item.objects_total} objects · {item.taken_at.slice(0, 16).replace("T", " ")}
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
              setParam({ mode: showDiff ? undefined : "diff" });
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
                  toast(describe(error), "error");
                });
            }}
          />
        )}
      </PageNotices>
      <PageBody pane={true} detail={false}>
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
            <ObjectPanel panel={panel} />
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
                toast(describe(error), "error");
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
                    toast(describe(error), "error");
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
}

export function ObjectPanel({ panel }: { panel: Panel }): ReactElement {
  if (panel.status === "empty") {
    return <EmptyState fill title="pick an object in the tree" />;
  }

  if (panel.status === "loading") {
    return <EmptyState fill title="loading the object" />;
  }

  if (panel.status === "failed") {
    return (
      <EmptyState fill title="the object is not available">
        {panel.message}
      </EmptyState>
    );
  }

  return <ObjectCardPanel card={panel.card} />;
}

function resolveVersion(latest: number, requested: number): number {
  if (requested < 0 || requested > latest) {
    return latest;
  }

  return requested;
}

type SyncBarProps = {
  sync: Sync;
  canCancel: boolean;
  onCancel: () => void;
};

/** Полоса последней синхронизации: ход с прогрессом и отменой либо итог. */
function SyncBar({ sync, canCancel, onCancel }: SyncBarProps): ReactElement {
  const started = sync.started_at.slice(0, 16).replace("T", " ");
  if (sync.status === "running") {
    const total = sync.objects_total === null ? "?" : String(sync.objects_total);
    return (
      <Alert tone="info" mark="sync-status">
        <span data-testid="sync-progress" data-status={sync.status}>
          syncing {sync.connection_name}: {sync.objects_done} / {total} objects
        </span>{" "}
        {canCancel && (
          <Button size="sm" tone="ghost" icon={XCircle} onClick={onCancel} data-testid="cancel-sync">
            cancel
          </Button>
        )}
      </Alert>
    );
  }

  if (sync.status === "done") {
    return (
      <Alert tone="ok" mark="sync-status">
        <span data-testid="sync-progress" data-status={sync.status}>
          synced v{sync.version} at {started}: {sync.objects_done} objects
        </span>
      </Alert>
    );
  }

  return (
    <Alert tone="error" mark="sync-status" title={`sync ${sync.status} at ${started}`}>
      <span data-testid="sync-progress" data-status={sync.status}>
        {sync.error}
      </span>
    </Alert>
  );
}

async function load(api: CatalogApi, connectionId: string): Promise<Loaded> {
  const access = await api.access();
  const kinds = await api.sourceKinds();
  const views = await api.connections();
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

export function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return String(error);
}
