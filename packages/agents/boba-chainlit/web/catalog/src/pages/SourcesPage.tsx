import { Link2, Pencil, PlugZap, Plus, Trash2, Unlink } from "lucide-react";
import { useCallback, useEffect, useState, type ReactElement } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import { AssignDialog } from "../components/connections/AssignDialog";
import { ConnectionDialog } from "../components/connections/ConnectionDialog";
import type { Access, ConnectionView, Source } from "../model/catalog";
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

type Lists = { access: Access; sources: Source[]; connections: ConnectionView[]; doc: SchemaDoc; kinds: string[] };
type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; lists: Lists };
type DialogState =
  | { kind: "none" }
  | { kind: "connection"; row: ConnectionView | null }
  | { kind: "assign"; row: ConnectionView }
  | { kind: "delete"; row: ConnectionView };
type Probe = { id: string; text: string; ok: boolean };

/** Подключения и источники: подключение заводится со всеми полями по схеме
 * api, затем помечается источником — именем и описанием; источник группирует
 * подключения одного вида. */
export function SourcesPage(): ReactElement {
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
      if (message.source_id !== null) {
        reload();
      }
    });
  }, [api, reload]);

  if (state.status === "loading") {
    return <EmptyState fill title="loading" />;
  }

  if (state.status === "failed") {
    return (
      <EmptyState fill title="sources are not available">
        {state.message}
      </EmptyState>
    );
  }

  const { access, sources, connections, doc, kinds } = state.lists;
  const sourceOf = (connection: ConnectionView): Source | undefined =>
    sources.find((source) => source.connection_ids.includes(connection.id));

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
    <Index mark="sources-page" data-can-edit={access.can_edit}>
      <IndexHead>
        <TopbarLink to="/">catalog</TopbarLink>
        <Eyebrow as="h4">sources</Eyebrow>
      </IndexHead>

      <Section
        title={`connections · ${connections.length}`}
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
        <Note>
          Add a connection with its credentials, then mark it with a source: a name for the place the data lives.
        </Note>
        <List kind="spaced" mark="connections-list" empty="no connections yet">
          {connections.map((row) => (
            <ConnectionRow
              key={row.id}
              row={row}
              source={sourceOf(row)}
              syncable={kinds.includes(row.kind)}
              canEdit={access.can_edit}
              probe={probe?.id === row.id ? probe : null}
              onOpen={() => {
                setDialog({ kind: "connection", row });
              }}
              onCheck={() => {
                check(row);
              }}
              onAssign={() => {
                setDialog({ kind: "assign", row });
              }}
              onUnassign={(source) => {
                run(api.unbindConnection(source.id, row.id), "connection unassigned");
              }}
              onDelete={() => {
                setDialog({ kind: "delete", row });
              }}
            />
          ))}
        </List>
      </Section>

      <Section title={`sources · ${sources.length}`}>
        <List kind="spaced" mark="sources-list" empty="no sources yet: assign a connection to create one">
          {sources.map((source) => (
            <ListRow key={source.id} data-source={source.name}>
              <ListName to={`/sources/${source.id}`}>{source.name}</ListName>
              <ListAside>
                <Chip tone="muted">{source.kind}</Chip>
                <Chip tone="muted">
                  {source.connection_ids.length} connection{source.connection_ids.length === 1 ? "" : "s"}
                </Chip>
                <Chip tone="muted">{source.latest_version === 0 ? "no versions" : `v${source.latest_version}`}</Chip>
                {source.description !== "" && (
                  <Note micro tone="faint">
                    {source.description}
                  </Note>
                )}
              </ListAside>
            </ListRow>
          ))}
        </List>
      </Section>

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
      {dialog.kind === "assign" && (
        <AssignDialog
          connection={dialog.row}
          sources={sources.filter((source) => source.kind === dialog.row.kind)}
          onAssign={(sourceId) => {
            run(api.bindConnection(sourceId, dialog.row.id), "connection assigned");
          }}
          onCreate={(spec) => {
            api
              .createSource({ ...spec, connection_id: dialog.row.id })
              .then((source) => {
                setDialog({ kind: "none" });
                toast("source created", "success");
                void navigate(`/sources/${source.id}`);
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
            The connection “{dialog.row.name}” will be deleted. A connection bound to a source is refused until
            unassigned.
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
  row: ConnectionView;
  source: Source | undefined;
  /** У вида подключения есть снимок: его можно ставить в источник. */
  syncable: boolean;
  canEdit: boolean;
  probe: Probe | null;
  onOpen: () => void;
  onCheck: () => void;
  onAssign: () => void;
  onUnassign: (source: Source) => void;
  onDelete: () => void;
};

/** Строка подключения: вид, владение, источник, итог проверки и действия. */
function ConnectionRow({
  row,
  source,
  syncable,
  canEdit,
  probe,
  onOpen,
  onCheck,
  onAssign,
  onUnassign,
  onDelete,
}: RowProps): ReactElement {
  return (
    <ListRow data-connection={row.name} data-source={source?.name}>
      <ListName title={row.available ? undefined : "the connection type is not installed"} onClick={onOpen}>
        {row.name}
      </ListName>
      <ListAside>
        <Chip tone="muted">{row.kind}</Chip>
        {!row.mine && <Chip tone="muted">shared</Chip>}
        {source !== undefined ? (
          <Chip tone="draft" mark="connection-source">
            {source.name}
          </Chip>
        ) : (
          <Chip tone="muted" mark="connection-source">
            no source
          </Chip>
        )}
        {probe !== null && (
          <Chip tone={probe.ok ? "draft" : "warn"} mark="probe-result" title={probe.text}>
            {probe.ok ? "connected" : "failed"}
          </Chip>
        )}
        <IconButton size="sm" ghost aria-label={`check ${row.name}`} onClick={onCheck}>
          <PlugZap size={14} />
        </IconButton>
        {canEdit && source === undefined && syncable && (
          <IconButton size="sm" ghost aria-label={`assign ${row.name} to a source`} onClick={onAssign}>
            <Link2 size={14} />
          </IconButton>
        )}
        {canEdit && source !== undefined && (
          <IconButton
            size="sm"
            ghost
            aria-label={`unassign ${row.name}`}
            onClick={() => {
              onUnassign(source);
            }}
          >
            <Unlink size={14} />
          </IconButton>
        )}
        {row.mine && (
          <IconButton size="sm" ghost aria-label={`edit ${row.name}`} onClick={onOpen}>
            <Pencil size={14} />
          </IconButton>
        )}
        {row.mine && (
          <IconButton size="sm" ghost aria-label={`delete ${row.name}`} onClick={onDelete}>
            <Trash2 size={14} />
          </IconButton>
        )}
      </ListAside>
    </ListRow>
  );
}

async function load(api: CatalogApi): Promise<Lists> {
  const access = await api.access();
  const sources = await api.sources();
  const connections = await api.connections();
  const kinds = await api.sourceKinds();
  const doc = new SchemaDoc(parseSchema(await api.connectionSchema()));
  return { access, sources, connections, doc, kinds };
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail;
  }

  return String(error);
}
