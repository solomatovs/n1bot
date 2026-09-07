import { Pencil, PlugZap, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState, type ReactElement } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import { ConnectionDialog } from "../components/connections/ConnectionDialog";
import { SyncDialog } from "../components/connections/SyncDialog";
import { connectionRows, type Access, type ConnectionRow, type ConnectionView, type SyncedConnection } from "../model/catalog";
import { SchemaDoc, parseSchema } from "../model/schema";
import {
  Alert,
  Button,
  Chip,
  Dialog,
  EmptyState,
  Eyebrow,
  IconButton,
  Index,
  IndexHead,
  List,
  ListAside,
  ListName,
  ListRow,
  Note,
  Section,
  Toolbar,
  TopbarLink,
  useToast,
} from "../ui";

type Lists = { access: Access; rows: ConnectionRow[]; foreign: SyncedConnection[]; doc: SchemaDoc };
type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; lists: Lists };
type DialogState =
  | { kind: "none" }
  | { kind: "connection"; row: ConnectionView | null }
  | { kind: "sync"; row: ConnectionView }
  | { kind: "delete"; row: ConnectionView };
type Probe = { id: string; text: string; ok: boolean };

/** Подключения: заводятся со всеми полями по схеме api, проверяются,
 * синхронизируются в каталог, правятся и удаляются; каталог показывает у
 * каждого последнюю версию снимка. */
export function ConnectionsPage(): ReactElement {
  const { api } = useServices();
  const toast = useToast();
  const navigate = useNavigate();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [dialog, setDialog] = useState<DialogState>({ kind: "none" });
  const [probe, setProbe] = useState<Probe | null>(null);

  const reload = useCallback(() => {
    let cancelled = false;
    load(api)
      .then((lists) => {
        if (!cancelled) {
          setState({ status: "ready", lists });
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
  }, [api]);

  useEffect(() => reload(), [reload]);

  useEffect(() => {
    return api.events((message) => {
      if (message.connection_id !== null) {
        reload();
      }
    });
  }, [api, reload]);

  if (state.status === "loading") {
    return <EmptyState fill title="loading" />;
  }

  if (state.status === "failed") {
    return (
      <EmptyState fill title="connections are not available">
        {state.message}
      </EmptyState>
    );
  }

  const { access, rows, foreign, doc } = state.lists;

  const run = (action: Promise<unknown>, done: string): void => {
    action
      .then(() => {
        setDialog({ kind: "none" });
        toast(done, "success");
        reload();
      })
      .catch((error: unknown) => {
        toast(describe(error), "error");
      });
  };

  const check = (row: ConnectionView): void => {
    setProbe(null);
    api
      .checkStoredConnection(row.id)
      .then((result) => {
        setProbe({ id: row.id, text: `${result.message} · ${result.elapsed_ms} ms`, ok: result.ok });
      })
      .catch((error: unknown) => {
        toast(describe(error), "error");
      });
  };

  return (
    <Index mark="connections-page" data-can-edit={access.can_edit}>
      <IndexHead>
        <TopbarLink to="/">catalog</TopbarLink>
        <Eyebrow as="h4">connections</Eyebrow>
      </IndexHead>

      <Section
        title={`connections · ${rows.length}`}
        actions={
          <Button
            size="sm"
            tone="primary"
            icon={Plus}
            onClick={() => {
              setDialog({ kind: "connection", row: null });
            }}
            data-testid="add-connection"
          >
            connection
          </Button>
        }
      >
        <Note>Add a connection, then sync it: the catalog learns its tables and they can go into a process.</Note>
        <List kind="spaced" mark="connections-list" empty="no connections yet">
          {rows.map((row) => (
            <ConnectionListRow
              key={row.view.id}
              row={row}
              canEdit={access.can_edit}
              probe={probe?.id === row.view.id ? probe : null}
              onOpen={() => {
                setDialog({ kind: "connection", row: row.view });
              }}
              onCheck={() => {
                check(row.view);
              }}
              onSync={() => {
                setDialog({ kind: "sync", row: row.view });
              }}
              onDelete={() => {
                setDialog({ kind: "delete", row: row.view });
              }}
            />
          ))}
        </List>
      </Section>

      {foreign.length > 0 && (
        <Section title={`synced by others · ${foreign.length}`}>
          <Note>Connections you cannot see, whose snapshots are in the catalog.</Note>
          <List kind="spaced" mark="foreign-list">
            {foreign.map((item) => (
              <ListRow key={item.connection_id} data-connection={item.name}>
                <ListName to={`/connections/${item.connection_id}`}>{item.name}</ListName>
                <ListAside>
                  <Chip tone="muted">{item.kind}</Chip>
                  <Chip tone="muted">v{item.latest_version}</Chip>
                </ListAside>
              </ListRow>
            ))}
          </List>
        </Section>
      )}

      {dialog.kind === "connection" && (
        <ConnectionDialog
          api={api}
          doc={doc}
          row={dialog.row}
          onSaved={() => {
            setDialog({ kind: "none" });
            toast("connection saved", "success");
            reload();
          }}
          onClose={() => {
            setDialog({ kind: "none" });
          }}
        />
      )}
      {dialog.kind === "sync" && (
        <SyncDialog
          connectionName={dialog.row.name}
          onStart={(scope) => {
            const id = dialog.row.id;
            api
              .startSync(id, scope)
              .then(() => {
                setDialog({ kind: "none" });
                toast("sync started", "success");
                void navigate(`/connections/${id}`);
              })
              .catch((error: unknown) => {
                toast(describe(error), "error");
              });
          }}
          onClose={() => {
            setDialog({ kind: "none" });
          }}
        />
      )}
      {dialog.kind === "delete" && (
        <Dialog
          title="delete the connection"
          mark="connection-delete"
          onClose={() => {
            setDialog({ kind: "none" });
          }}
        >
          <Alert tone="info">
            The connection “{dialog.row.name}” will be deleted. A connection with catalog versions or with nodes in a
            process is refused with the reason.
          </Alert>
          <Toolbar>
            <Button
              tone="danger"
              onClick={() => {
                run(api.removeConnection(dialog.row.id), "connection deleted");
              }}
              data-testid="delete-connection"
            >
              delete the connection
            </Button>
            <Button
              tone="ghost"
              onClick={() => {
                setDialog({ kind: "none" });
              }}
            >
              cancel
            </Button>
          </Toolbar>
        </Dialog>
      )}
    </Index>
  );
}

type RowProps = {
  row: ConnectionRow;
  canEdit: boolean;
  probe: Probe | null;
  onOpen: () => void;
  onCheck: () => void;
  onSync: () => void;
  onDelete: () => void;
};

/** Строка подключения: вид, владение, версия снимка, итог проверки и
 * действия: check, sync, edit, delete. */
function ConnectionListRow({ row, canEdit, probe, onOpen, onCheck, onSync, onDelete }: RowProps): ReactElement {
  const { view, synced, syncable } = row;
  return (
    <ListRow data-connection={view.name} data-synced={synced !== undefined}>
      <ListName to={`/connections/${view.id}`} title={view.available ? undefined : "the connection type is not installed"}>
        {view.name}
      </ListName>
      <ListAside>
        <Chip tone="muted">{view.kind}</Chip>
        {!view.mine && <Chip tone="muted">shared</Chip>}
        {synced !== undefined && (
          <Chip tone="draft" mark="connection-version">
            v{synced.latest_version}
          </Chip>
        )}
        {synced === undefined && syncable && (
          <Chip tone="muted" mark="connection-version">
            not synced
          </Chip>
        )}
        {probe !== null && (
          <Chip tone={probe.ok ? "draft" : "warn"} mark="probe-result" title={probe.text}>
            {probe.ok ? "connected" : "failed"}
          </Chip>
        )}
        <IconButton size="sm" ghost aria-label={`check ${view.name}`} title="check" onClick={onCheck}>
          <PlugZap size={14} />
        </IconButton>
        {canEdit && syncable && (
          <IconButton size="sm" ghost aria-label={`sync ${view.name}`} title="sync" onClick={onSync}>
            <RefreshCw size={14} />
          </IconButton>
        )}
        {view.mine && (
          <IconButton size="sm" ghost aria-label={`edit ${view.name}`} title="edit" onClick={onOpen}>
            <Pencil size={14} />
          </IconButton>
        )}
        {view.mine && (
          <IconButton size="sm" ghost aria-label={`delete ${view.name}`} title="delete" onClick={onDelete}>
            <Trash2 size={14} />
          </IconButton>
        )}
      </ListAside>
    </ListRow>
  );
}

async function load(api: CatalogApi): Promise<Lists> {
  const access = await api.access();
  const views = await api.connections();
  const kinds = await api.sourceKinds();
  const synced = await api.synced();
  const doc = new SchemaDoc(parseSchema(await api.connectionSchema()));
  const { rows, foreign } = connectionRows(views, kinds, synced);
  return { access, rows, foreign, doc };
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail;
  }

  return String(error);
}
