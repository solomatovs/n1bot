import { GitCompare, Link2, RefreshCw, Trash2, XCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import { ConnectionsDialog, connectionLabel } from "../components/sources/ConnectionsDialog";
import { DiffPanel } from "../components/sources/DiffPanel";
import { ObjectCardPanel } from "../components/sources/ObjectCardPanel";
import { SourceTree } from "../components/sources/SourceTree";
import { SyncDialog } from "../components/sources/SyncDialog";
import type {
  Access,
  ConnectionView,
  ObjectCard,
  ObjectRef,
  Source,
  SourceConnection,
  SourceDiff,
  SourceVersion,
  Sync,
  TreeNode,
} from "../model/catalog";
import { RefParam } from "../model/refParam";
import {
  Alert,
  Button,
  Chip,
  Dialog,
  EmptyState,
  IconButton,
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

type Directory = { entries: ConnectionView[]; error: string | null };
type Loaded = {
  access: Access;
  source: Source;
  versions: SourceVersion[];
  connections: SourceConnection[];
  directory: Directory;
  syncs: Sync[];
};
type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; loaded: Loaded };
type Panel =
  | { status: "empty" }
  | { status: "loading" }
  | { status: "failed"; message: string }
  | { status: "card"; card: ObjectCard };

/** Страница источника: дерево версии слева, родная карточка объекта справа,
 * выбор версии и разница с предыдущей, у ручного источника — черновики. */
export function SourcePage(): ReactElement {
  const { sourceId } = useParams();
  if (sourceId === undefined) {
    return <EmptyState fill title="source id is missing" />;
  }

  return <SourceView sourceId={sourceId} />;
}

function SourceView({ sourceId }: { sourceId: string }): ReactElement {
  const { api } = useServices();
  const toast = useToast();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [panel, setPanel] = useState<Panel>({ status: "empty" });
  const [diff, setDiff] = useState<SourceDiff | null>(null);
  const [dialog, setDialog] = useState<"none" | "delete" | "connections" | "sync">("none");
  const [reloads, setReloads] = useState(0);
  const [latestSync, setLatestSync] = useState<Sync | null>(null);

  const requestedVersion = Number(params.get("v") ?? "-1");
  const showDiff = params.get("mode") === "diff";
  const selected = useMemo(() => RefParam.parse(params.get("ref")), [params]);

  const reload = useCallback(() => {
    let cancelled = false;
    load(api, sourceId)
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
  }, [api, sourceId]);

  useEffect(() => reload(), [reload]);

  useEffect(() => {
    return api.events((message) => {
      if (message.source_id === sourceId) {
        reload();
        return;
      }

      if (message.sync_id !== null) {
        const syncId = message.sync_id;
        api
          .sync(syncId)
          .then((sync) => {
            if (sync.source_id === sourceId) {
              setLatestSync(sync);
            }
          })
          .catch((error: unknown) => {
            toast(describe(error), "error");
          });
      }
    });
  }, [api, sourceId, reload, toast]);

  const version = state.status === "ready" ? resolveVersion(state.loaded.source, requestedVersion) : requestedVersion;

  const loadTree = useCallback((path: string[]) => api.sourceTree(sourceId, version, path), [api, sourceId, version]);

  useEffect(() => {
    if (selected === undefined) {
      setPanel({ status: "empty" });
      return;
    }

    let cancelled = false;
    setPanel({ status: "loading" });
    api
      .sourceObject(sourceId, version, selected.kind, selected.path)
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
  }, [api, sourceId, version, selected, reloads]);

  useEffect(() => {
    if (!showDiff || version < 2) {
      setDiff(null);
      return;
    }

    let cancelled = false;
    api
      .sourceDiff(sourceId, version - 1, version)
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
  }, [api, sourceId, version, showDiff, toast, reloads]);

  if (state.status === "loading") {
    return <EmptyState fill title="loading the source" />;
  }

  if (state.status === "failed") {
    return (
      <EmptyState fill title="the source is not available">
        {state.message}
      </EmptyState>
    );
  }

  const { access, source, versions, connections, directory } = state.loaded;
  const canSync = access.can_edit;
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
    <Page mark="source-page" data-source={source.name} data-version={version} data-can-edit={access.can_edit}>
      <Topbar>
        <TopbarLink to="/">catalog</TopbarLink>
        <TopbarLink to="/sources">sources</TopbarLink>
        <TopbarTitle>{source.name}</TopbarTitle>
        <Chip tone="muted">{source.kind}</Chip>
        <Select
          aria-label="source version"
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
          <Button
            size="sm"
            tone="ghost"
            icon={Link2}
            collapsible
            title="connections"
            onClick={() => {
              setDialog("connections");
            }}
            data-testid="source-connections"
          >
            connections · {connections.length}
          </Button>
          {canSync && (
            <Button
              size="sm"
              tone="primary"
              icon={RefreshCw}
              disabled={latestSync?.status === "running"}
              onClick={() => {
                setDialog("sync");
              }}
              data-testid="source-sync"
            >
              sync
            </Button>
          )}
          {access.can_edit && (
            <IconButton
              aria-label="delete source"
              onClick={() => {
                setDialog("delete");
              }}
            >
              <Trash2 size={14} />
            </IconButton>
          )}
        </TopbarGroup>
        <TopbarSpacer />
        <TopbarHint>{source.description}</TopbarHint>
      </Topbar>
      <PageNotices>
        {latestSync !== null && (
          <SyncBar
            sync={latestSync}
            label={connectionLabel(latestSync.connection_id, directory.entries)}
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
            <EmptyState title="no versions yet">bind a connection and run a synchronisation</EmptyState>
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
      {dialog === "connections" && (
        <ConnectionsDialog
          sourceName={source.name}
          bound={connections}
          directory={directory.entries}
          directoryError={directory.error}
          canEdit={access.can_edit}
          onBind={(connectionId) => {
            api
              .bindConnection(source.id, connectionId)
              .then(() => {
                toast("connection bound", "success");
                reload();
              })
              .catch((error: unknown) => {
                toast(describe(error), "error");
              });
          }}
          onUnbind={(connectionId) => {
            api
              .unbindConnection(source.id, connectionId)
              .then(() => {
                toast("connection unbound", "success");
                reload();
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
      {dialog === "sync" && (
        <SyncDialog
          sourceName={source.name}
          bound={connections}
          directory={directory.entries}
          onStart={(connectionId, scope) => {
            api
              .startSync(source.id, connectionId, scope)
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
      {dialog === "delete" && (
        <Dialog
          title="delete the source"
          mark="source-delete"
          onClose={() => {
            setDialog("none");
          }}
        >
          <Alert tone="info">The source “{source.name}” with all its versions will be deleted.</Alert>
          <Toolbar>
            <Button
              tone="danger"
              onClick={() => {
                api
                  .deleteSource(source.id)
                  .then(() => {
                    toast("source deleted", "success");
                    void navigate("/sources");
                  })
                  .catch((error: unknown) => {
                    toast(describe(error), "error");
                  });
              }}
            >
              delete the source
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

function resolveVersion(source: Source, requested: number): number {
  if (requested < 0 || requested > source.latest_version) {
    return source.latest_version;
  }

  return requested;
}

type SyncBarProps = {
  sync: Sync;
  label: string;
  canCancel: boolean;
  onCancel: () => void;
};

/** Полоса последней синхронизации: ход с прогрессом и отменой либо итог. */
function SyncBar({ sync, label, canCancel, onCancel }: SyncBarProps): ReactElement {
  const started = sync.started_at.slice(0, 16).replace("T", " ");
  if (sync.status === "running") {
    const total = sync.objects_total === null ? "?" : String(sync.objects_total);
    return (
      <Alert tone="info" mark="sync-status">
        <span data-testid="sync-progress" data-status={sync.status}>
          syncing via {label}: {sync.objects_done} / {total} objects
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
          synced v{sync.version} via {label} at {started}: {sync.objects_done} objects
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

async function load(api: CatalogApi, sourceId: string): Promise<Loaded> {
  const access = await api.access();
  const source = await api.source(sourceId);
  const versions = await api.sourceVersions(sourceId);
  const connections = await api.sourceConnections(sourceId);
  const syncs = await api.sourceSyncs(sourceId);
  const directory = await loadDirectory(api, source.kind);

  return { access, source, versions, connections, directory, syncs };
}

/** Справочник подключений вида; отказ брокера не роняет страницу, а
 * показывается в диалогах. */
async function loadDirectory(api: CatalogApi, kind: string): Promise<Directory> {
  try {
    return { entries: await api.connections(kind), error: null };
  } catch (error: unknown) {
    return { entries: [], error: describe(error) };
  }
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

export type { ObjectRef };
