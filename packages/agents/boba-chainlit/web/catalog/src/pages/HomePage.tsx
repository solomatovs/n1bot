import { PanelLeft, Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import { ConnectionDialog } from "../components/connections/ConnectionDialog";
import { NewProcessDialog } from "../components/edit/NewProcessDialog";
import { LeftPane } from "../components/LeftPane";
import { ObjectPanel } from "../components/ObjectPanel";
import { Catalog, type Access, type Draft, type Process } from "../model/catalog";
import { SchemaDoc, parseSchema } from "../model/schema";
import { readUrlState, writeUrlState, type UrlState } from "../model/urlState";
import {
  Button,
  Detail,
  EmptyState,
  IconButton,
  Page,
  PageBody,
  Pane,
  Scene,
  Topbar,
  TopbarHint,
  TopbarSpacer,
  TopbarTitle,
  useToast,
} from "../ui";

type Lists = { access: Access; processes: Process[]; drafts: Draft[] };
type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; lists: Lists };
type DialogState = { kind: "new-process" } | { kind: "connection"; doc: SchemaDoc };

/** Вход в каталог: та же страница, что у процесса, но без открытого
 * процесса — в панели все процессы и подключения, на сцене подсказка. */
export function HomePage(): ReactElement {
  const { api } = useServices();
  const toast = useToast();
  const navigate = useNavigate();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [params, setParams] = useSearchParams();
  const [paneOpen, setPaneOpen] = useState(() => !narrowScreen());
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const url = useMemo(() => readUrlState(params), [params]);
  const emptyCatalog = useMemo(() => new Catalog({ groups: {}, nodes: {}, flows: {} }), []);

  const update = useCallback(
    (patch: Partial<UrlState>) => {
      setParams((current) => writeUrlState({ ...readUrlState(current), ...patch }, current), { replace: true });
    },
    [setParams],
  );

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
      if (message.process_id !== null || message.draft_id !== null) {
        reload();
      }
    });
  }, [api, reload]);

  if (state.status === "loading") {
    return <EmptyState fill title="loading the catalog" />;
  }

  if (state.status === "failed") {
    return (
      <EmptyState fill title="the catalog is not available">
        {state.message}
      </EmptyState>
    );
  }

  const { access, processes, drafts } = state.lists;

  const openConnectionDialog = (): void => {
    api
      .connectionSchema()
      .then((schema) => {
        setDialog({ kind: "connection", doc: new SchemaDoc(parseSchema(schema)) });
      })
      .catch((error: unknown) => {
        toast(describe(error), "error");
      });
  };

  const newProcess = access.can_edit
    ? () => {
        setDialog({ kind: "new-process" });
      }
    : undefined;

  const actions = newProcess === undefined ? null : (
    <Button size="sm" tone="primary" icon={Plus} onClick={newProcess} data-testid="new-process">
      process
    </Button>
  );

  return (
    <Page mark="catalog-page" data-source="home" data-can-edit={access.can_edit}>
      <Topbar>
        <IconButton
          aria-label={paneOpen ? "hide the left pane" : "show the left pane"}
          aria-pressed={paneOpen}
          onClick={() => {
            setPaneOpen((open) => !open);
          }}
        >
          <PanelLeft size={14} />
        </IconButton>
        <TopbarTitle>processes</TopbarTitle>
        <TopbarSpacer />
        <TopbarHint>
          {processes.length} process{processes.length === 1 ? "" : "es"}
        </TopbarHint>
      </Topbar>
      <PageBody pane={paneOpen} detail={url.object !== undefined}>
        {paneOpen && (
          <Pane>
            <LeftPane
              api={api}
              guest={false}
              tab={url.pane}
              onTab={(tab) => {
                update({ pane: tab });
              }}
              actions={actions}
              processes={processes}
              drafts={drafts}
              currentDraftId={undefined}
              onNewProcess={newProcess}
              open={undefined}
              onAddConnection={access.can_edit ? openConnectionDialog : undefined}
              selectedObject={url.object}
              onSelectObject={(ref) => {
                update({ object: ref });
              }}
            />
          </Pane>
        )}
        <Scene>
          <EmptyState fill title="pick a process on the left or press “process” to start one" mark="home-hint" />
        </Scene>
        {url.object !== undefined && (
          <Detail>
            <ObjectPanel
              key={`${url.object.connection_id}:${url.object.kind}:${url.object.path.join("/")}`}
              api={api}
              catalog={emptyCatalog}
              object={url.object}
              editing={undefined}
              retargetFor={undefined}
              onOpenNode={() => {
                update({ object: undefined });
              }}
              onOpenObject={(ref) => {
                update({ object: ref });
              }}
              onClose={() => {
                update({ object: undefined });
              }}
            />
          </Detail>
        )}
      </PageBody>
      {dialog?.kind === "new-process" && (
        <NewProcessDialog
          api={api}
          onCreated={(created) => {
            setDialog(null);
            void navigate(`/drafts/${created.id}`);
          }}
          onClose={() => {
            setDialog(null);
          }}
        />
      )}
      {dialog?.kind === "connection" && (
        <ConnectionDialog
          api={api}
          doc={dialog.doc}
          row={null}
          onSaved={(saved) => {
            setDialog(null);
            toast(`connection ${saved.name} saved; sync it to see its tables`, "success");
            void navigate(`/connections/${saved.id}`);
          }}
          onClose={() => {
            setDialog(null);
          }}
        />
      )}
    </Page>
  );
}

/** Узкий экран: панель по умолчанию закрыта, как на странице процесса. */
const NARROW_MAX_WIDTH = 900;

function narrowScreen(): boolean {
  return window.matchMedia(`(max-width: ${NARROW_MAX_WIDTH}px)`).matches;
}

async function load(api: CatalogApi): Promise<Lists> {
  const access = await api.access();
  const processes = await api.processes();
  const drafts = await api.myDrafts();
  return { access, processes, drafts };
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail;
  }

  return String(error);
}
