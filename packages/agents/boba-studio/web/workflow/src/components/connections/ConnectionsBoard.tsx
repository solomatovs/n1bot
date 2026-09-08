import { Eye, Pencil, PlugZap, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState, type ReactElement, type ReactNode } from "react";

import { ApiError } from "../../api/transport";
import { Async } from "../Async";
import { useLoadable } from "../../hooks/useLoadable";
import type { ConnectionView } from "../../model/account";
import { SchemaDoc, parseSchema } from "../../model/schema";
import { useServices } from "../../services";
import {
  Alert,
  Button,
  Chip,
  Dialog,
  IconButton,
  List,
  ListAside,
  ListName,
  ListRow,
  Note,
  Section,
  Toolbar,
  useToast,
} from "../../ui";
import { ConnectionForm } from "./ConnectionForm";

/** Что каталог добавляет строке: ссылка на страницу снимка, пометка
 * «синхронизировано», чипы версии и кнопка sync. */
export type RowExtension = {
  href: string;
  synced: boolean;
  marks: ReactNode;
  actions: ReactNode;
};

/** Расширение доски каталогом; без него — голый список соединений брокера. */
export type CatalogExtension = {
  row: (view: ConnectionView) => RowExtension;
  /** Секции под списком по видимым соединениям: синхронизированные другими. */
  below: (views: ConnectionView[]) => ReactNode;
};

type Props = {
  catalog?: CatalogExtension | undefined;
};

type Loaded = { doc: SchemaDoc; views: ConnectionView[] };
type DialogState =
  | { kind: "none" }
  | { kind: "connection"; row: ConnectionView | null }
  | { kind: "delete"; row: ConnectionView };
type Probe = { id: string; text: string; ok: boolean };

/** Доска соединений пользователя: свои и общие группами, пометка типа без
 * пакета, проверка, форма создания и правки в диалоге, удаление с
 * подтверждением; перечитывается по connections_changed из ленты. Одна на
 * страницу соединений studio, каталог дополняет строки через CatalogExtension. */
export function ConnectionsBoard({ catalog }: Props): ReactElement {
  const { api, socket } = useServices();
  const toast = useToast();
  const [dialog, setDialog] = useState<DialogState>({ kind: "none" });
  const [probe, setProbe] = useState<Probe | null>(null);

  const [state, reload] = useLoadable(
    useCallback(async () => {
      const [schema, views] = await Promise.all([api.connectionSchema(), api.connections()]);
      return { doc: new SchemaDoc(parseSchema(schema)), views };
    }, [api]),
    { keep: true },
  );

  useEffect(
    () =>
      socket.onUser((event) => {
        if (event.kind === "connections_changed") {
          reload();
        }
      }),
    [socket, reload],
  );

  const close = (): void => {
    setDialog({ kind: "none" });
  };

  const check = (row: ConnectionView): void => {
    setProbe(null);
    api
      .checkStoredConnection(row.id)
      .then((result) => {
        setProbe({ id: row.id, text: `${result.message} · ${result.elapsed_ms} ms`, ok: result.ok });
      })
      .catch((error: unknown) => {
        toast(ApiError.describe(error), "error");
      });
  };

  const remove = (row: ConnectionView): void => {
    api
      .removeConnection(row.id)
      .then(() => {
        close();
        toast("connection deleted", "success");
        reload();
      })
      .catch((error: unknown) => {
        toast(ApiError.describe(error), "error");
      });
  };

  const renderBoard = ({ doc, views }: Loaded): ReactElement => {
    const mine = views.filter((view) => view.mine);
    const shared = views.filter((view) => !view.mine);

    const rowOf = (view: ConnectionView): ReactElement => {
      let extension: RowExtension | undefined = undefined;
      if (catalog !== undefined) {
        extension = catalog.row(view);
      }

      let rowProbe: Probe | null = null;
      if (probe?.id === view.id) {
        rowProbe = probe;
      }

      return (
        <ConnectionListRow
          key={view.id}
          view={view}
          extension={extension}
          probe={rowProbe}
          onCheck={() => {
            check(view);
          }}
          onOpen={() => {
            setDialog({ kind: "connection", row: view });
          }}
          onDelete={() => {
            setDialog({ kind: "delete", row: view });
          }}
        />
      );
    };

    return (
      <>
        <div data-testid="connections-list">
          <Section
            title={`mine · ${mine.length}`}
            mark="connections-mine"
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
            <List kind="spaced" mark="mine-list" empty="no connections yet">
              {mine.map(rowOf)}
            </List>
          </Section>
          <Section title={`shared · ${shared.length}`} mark="connections-shared">
            <Note>Connections granted to your roles: read-only, check and use them.</Note>
            <List kind="spaced" mark="shared-list" empty="no shared connections">
              {shared.map(rowOf)}
            </List>
          </Section>
        </div>
        {catalog?.below(views)}

        {dialog.kind === "connection" && (
          <ConnectionDialog
            doc={doc}
            row={dialog.row}
            onSaved={() => {
              close();
              toast("connection saved", "success");
              reload();
            }}
            onClose={close}
          />
        )}
        {dialog.kind === "delete" && (
          <Dialog title="delete the connection" mark="connection-delete" onClose={close}>
            <Alert tone="info">
              The connection “{dialog.row.name}” will be deleted. A connection with catalog versions or with nodes in a
              process is refused with the reason.
            </Alert>
            <Toolbar>
              <Button
                tone="danger"
                onClick={() => {
                  remove(dialog.row);
                }}
                data-testid="delete-connection"
              >
                delete the connection
              </Button>
              <Button tone="ghost" onClick={close}>
                cancel
              </Button>
            </Toolbar>
          </Dialog>
        )}
      </>
    );
  };

  return (
    <Async
      state={state}
      fill
      title={{ loading: "loading connections", failed: "connections are not available" }}
      render={renderBoard}
    />
  );
}

type DialogProps = {
  doc: SchemaDoc;
  row: ConnectionView | null;
  onSaved: (saved: ConnectionView) => void;
  onClose: () => void;
};

/** Общая форма соединения в модальном окне. */
export function ConnectionDialog({ doc, row, onSaved, onClose }: DialogProps): ReactElement {
  let title = "new connection";
  if (row !== null) {
    title = row.name;
  }

  return (
    <Dialog title={title} mark="connection-form" wide onClose={onClose}>
      <ConnectionForm doc={doc} row={row} onSaved={onSaved} onClose={onClose} />
    </Dialog>
  );
}

type RowProps = {
  view: ConnectionView;
  extension: RowExtension | undefined;
  probe: Probe | null;
  onCheck: () => void;
  onOpen: () => void;
  onDelete: () => void;
};

/** Строка соединения: имя (ссылка на страницу снимка, если есть каталог),
 * вид, пометки, итог проверки и действия: check, edit или view (общее —
 * только чтение), delete. */
function ConnectionListRow({ view, extension, probe, onCheck, onOpen, onDelete }: RowProps): ReactElement {
  let title: string | undefined = undefined;
  if (!view.available) {
    title = "the connection type is not installed";
  }

  return (
    <ListRow data-connection={view.name} data-synced={extension?.synced} data-available={view.available}>
      <ListName to={extension?.href} title={title}>
        {view.name}
      </ListName>
      <ListAside>
        <Chip tone="muted">{view.kind}</Chip>
        {!view.available && (
          <Chip tone="warn" mark="connection-missing">
            not installed
          </Chip>
        )}
        {!view.mine && <Chip tone="muted">shared</Chip>}
        {extension?.marks}
        {probe !== null && <ProbeChip probe={probe} />}
        <IconButton size="sm" ghost aria-label={`check ${view.name}`} title="check" onClick={onCheck}>
          <PlugZap size={14} />
        </IconButton>
        {extension?.actions}
        {view.mine && view.available && (
          <IconButton size="sm" ghost aria-label={`edit ${view.name}`} title="edit" onClick={onOpen}>
            <Pencil size={14} />
          </IconButton>
        )}
        {!view.mine && view.available && (
          <IconButton size="sm" ghost aria-label={`view ${view.name}`} title="view" onClick={onOpen}>
            <Eye size={14} />
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

function ProbeChip({ probe }: { probe: Probe }): ReactElement {
  let tone: "draft" | "warn" = "warn";
  let text = "failed";
  if (probe.ok) {
    tone = "draft";
    text = "connected";
  }

  return (
    <Chip tone={tone} mark="probe-result" title={probe.text}>
      {text}
    </Chip>
  );
}
