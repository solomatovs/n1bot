import { RefreshCw } from "lucide-react";
import { useCallback, useMemo, useState, type ReactElement, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "../../api/transport";
import { ConnectionsBoard, type CatalogExtension, type RowExtension } from "../../components/connections/ConnectionsBoard";
import { PageUrls } from "../../config";
import { useLoadable } from "../../hooks/useLoadable";
import type { ConnectionView } from "../../model/account";
import type { CatalogChanged } from "../../model/workflow";
import { useServices } from "../../services";
import { CatalogApi } from "../api/client";
import { SyncDialog } from "../components/connections/SyncDialog";
import type { Access, SyncedConnection } from "../model/catalog";
import { useCatalogChanges } from "../services";
import { Chip, Eyebrow, IconButton, Index, IndexHead, List, ListAside, ListName, ListRow, Note, Section, TopbarLink, useToast } from "../../ui";

type Snapshots = { kinds: string[]; synced: SyncedConnection[] };

/** Доступ к каталогу; null — каталог выключен (503). */
async function loadAccess(api: CatalogApi): Promise<Access | null> {
  try {
    return await api.access();
  } catch (error: unknown) {
    if (error instanceof ApiError && error.status === 503) {
      return null;
    }

    throw error;
  }
}

/** Соединения глазами каталога: доска соединений плюс версии снимков, sync и
 * синхронизированные другими. Без каталога (503, нет доступа) — голая
 * доска: соединения нужны и без него. */
export function CatalogConnections(): ReactElement {
  const { transport } = useServices();
  const api = useMemo(() => new CatalogApi(transport), [transport]);
  const [access] = useLoadable(useCallback(() => loadAccess(api), [api]));

  if (access.kind === "loading") {
    return <Frame title="loading connections" />;
  }

  if (access.kind === "error") {
    return (
      <Frame>
        <Note tone="error">catalog is not available: {access.message}</Note>
        <ConnectionsBoard />
      </Frame>
    );
  }

  if (access.value === null) {
    return (
      <Frame>
        <ConnectionsBoard />
      </Frame>
    );
  }

  return <WithCatalog api={api} access={access.value} />;
}

type FrameProps = { title?: string; canEdit?: boolean | undefined; children?: ReactNode };

function Frame({ title, canEdit, children }: FrameProps): ReactElement {
  return (
    <Index mark="connections-page" data-can-edit={canEdit}>
      <IndexHead>
        <TopbarLink to={PageUrls.workflow()}>studio</TopbarLink>
        <TopbarLink to={PageUrls.catalog.home()}>catalog</TopbarLink>
        <Eyebrow as="h4">connections</Eyebrow>
      </IndexHead>
      {title !== undefined && <Note>{title}</Note>}
      {children}
    </Index>
  );
}

function WithCatalog({ api, access }: { api: CatalogApi; access: Access }): ReactElement {
  const toast = useToast();
  const navigate = useNavigate();
  const [syncing, setSyncing] = useState<ConnectionView | null>(null);

  const [snapshots, reload] = useLoadable(
    useCallback(async (): Promise<Snapshots> => {
      const [kinds, synced] = await Promise.all([api.sourceKinds(), api.synced()]);
      return { kinds, synced };
    }, [api]),
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

  const extension = useMemo<CatalogExtension | undefined>(() => {
    if (snapshots.kind !== "ready") {
      return undefined;
    }

    return catalogExtension(snapshots.value, access, setSyncing);
  }, [snapshots, access]);

  return (
    <Frame canEdit={access.can_edit}>
      <Note>Add a connection, then sync it: the catalog learns its tables and they can go into a process.</Note>
      <ConnectionsBoard catalog={extension} />
      {syncing !== null && (
        <SyncDialog
          connectionName={syncing.name}
          onStart={(scope) => {
            const id = syncing.id;
            api
              .startSync(id, scope)
              .then(() => {
                setSyncing(null);
                toast("sync started", "success");
                void navigate(PageUrls.catalog.connection(id));
              })
              .catch((error: unknown) => {
                toast(ApiError.describe(error), "error");
              });
          }}
          onClose={() => {
            setSyncing(null);
          }}
        />
      )}
    </Frame>
  );
}

/** Дополнение строк каталогом: версия снимка или «not synced» для
 * синхронизируемого вида, кнопка sync у редактора; ниже — чужие
 * соединения, чьи снимки есть в каталоге. */
function catalogExtension(
  snapshots: Snapshots,
  access: Access,
  onSync: (view: ConnectionView) => void,
): CatalogExtension {
  const byId = new Map(snapshots.synced.map((item) => [item.connection_id, item]));

  const row = (view: ConnectionView): RowExtension => {
    const synced = byId.get(view.id);
    const syncable = snapshots.kinds.includes(view.kind);

    let marks: ReactElement | null = null;
    if (synced !== undefined) {
      marks = (
        <Chip tone="draft" mark="connection-version">
          v{synced.latest_version}
        </Chip>
      );
    } else if (syncable) {
      marks = (
        <Chip tone="muted" mark="connection-version">
          not synced
        </Chip>
      );
    }

    let actions: ReactElement | null = null;
    if (access.can_edit && syncable) {
      actions = (
        <IconButton
          size="sm"
          ghost
          aria-label={`sync ${view.name}`}
          title="sync"
          onClick={() => {
            onSync(view);
          }}
        >
          <RefreshCw size={14} />
        </IconButton>
      );
    }

    return { href: PageUrls.catalog.connection(view.id), synced: synced !== undefined, marks, actions };
  };

  const below = (views: ConnectionView[]): ReactElement | null => {
    const seen = new Set(views.map((view) => view.id));
    return <ForeignSection synced={snapshots.synced} seen={seen} />;
  };

  return { row, below };
}

/** Соединения, которых пользователь не видит, но их снимки в каталоге есть. */
function ForeignSection({ synced, seen }: { synced: SyncedConnection[]; seen: ReadonlySet<string> }): ReactElement | null {
  const foreign = synced.filter((item) => !seen.has(item.connection_id));
  if (foreign.length === 0) {
    return null;
  }

  return (
    <Section title={`synced by others · ${foreign.length}`}>
      <Note>Connections you cannot see, whose snapshots are in the catalog.</Note>
      <List kind="spaced" mark="foreign-list">
        {foreign.map((item) => (
          <ListRow key={item.connection_id} data-connection={item.name}>
            <ListName to={PageUrls.catalog.connection(item.connection_id)}>{item.name}</ListName>
            <ListAside>
              <Chip tone="muted">{item.kind}</Chip>
              <Chip tone="muted">v{item.latest_version}</Chip>
            </ListAside>
          </ListRow>
        ))}
      </List>
    </Section>
  );
}
