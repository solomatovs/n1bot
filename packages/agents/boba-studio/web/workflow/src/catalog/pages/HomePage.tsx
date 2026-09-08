import { PanelLeft } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api/transport";
import { PageUrls } from "../../config";
import type { CatalogChanged } from "../../model/workflow";
import { Async } from "../../components/Async";
import { useLoadable } from "../../hooks/useLoadable";
import type { CatalogApi } from "../api/client";
import { useCatalog, useCatalogChanges } from "../services";
import { ConnectionDialog } from "../components/connections/ConnectionDialog";
import { NewProcessDialog } from "../components/edit/NewProcessDialog";
import { LeftPane } from "../components/LeftPane";
import { ObjectPanel } from "../components/ObjectPanel";
import { UpgradeBar } from "../components/tasks/TaskBar";
import { Catalog, type Access, type Draft, type Process, type UpgradeRun } from "../model/catalog";
import { SchemaDoc, parseSchema } from "../../model/schema";
import { readUrlState, writeUrlState, type UrlState } from "../model/urlState";
import {
  Detail,
  EmptyState,
  IconButton,
  Page,
  PageBody,
  PageNotices,
  Pane,
  Scene,
  Topbar,
  TopbarHint,
  TopbarSpacer,
  TopbarTitle,
  narrowScreen,
  useToast,
} from "../../ui";

type Lists = { access: Access; processes: Process[]; drafts: Draft[]; latestRun: UpgradeRun | undefined };
type DialogState = { kind: "new-process" } | { kind: "connection"; doc: SchemaDoc };

/** Вход в каталог: та же страница, что у процесса, но без открытого
 * процесса — в панели все процессы и подключения, на сцене подсказка. */
export function HomePage(): ReactElement {
  const { api, workflowApi } = useCatalog();
  const toast = useToast();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [paneOpen, setPaneOpen] = useState(() => !narrowScreen());
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [latestRun, setLatestRun] = useState<UpgradeRun | undefined>(undefined);
  const url = useMemo(() => readUrlState(params), [params]);
  const emptyCatalog = useMemo(() => new Catalog({ groups: {}, nodes: {}, flows: {} }), []);

  const update = useCallback(
    (patch: Partial<UrlState>) => {
      setParams((current) => writeUrlState({ ...readUrlState(current), ...patch }, current), { replace: true });
    },
    [setParams],
  );

  const [state, reload] = useLoadable(
    useCallback(() => load(api), [api]),
    { keep: true },
  );

  useEffect(() => {
    if (state.kind === "ready") {
      setLatestRun(state.value.latestRun);
    }
  }, [state]);

  useCatalogChanges(
    useCallback(
      (message: CatalogChanged) => {
        if (message.upgrade_id !== null) {
          api
            .upgradeRun(message.upgrade_id)
            .then((run) => {
              setLatestRun(run);
              if (run.status !== "running") {
                reload();
              }
            })
            .catch((error: unknown) => {
              toast(ApiError.describe(error), "error");
            });
          return;
        }

        if (message.process_id !== null || message.draft_id !== null) {
          reload();
        }
      },
      [api, reload, toast],
    ),
  );

  const renderHome = ({ access, processes, drafts }: Lists): ReactElement => {
    const openConnectionDialog = (): void => {
      workflowApi
        .connectionSchema()
        .then((schema) => {
          setDialog({ kind: "connection", doc: new SchemaDoc(parseSchema(schema)) });
        })
        .catch((error: unknown) => {
          toast(ApiError.describe(error), "error");
        });
    };

    const newProcess = access.can_edit
      ? () => {
          setDialog({ kind: "new-process" });
        }
      : undefined;

    const upgradeAll = (): void => {
      api
        .upgradeAll()
        .then((run) => {
          setLatestRun(run);
          toast("upgrade of all lagging processes started", "success");
        })
        .catch((error: unknown) => {
          toast(ApiError.describe(error), "error");
        });
    };

    const cancelUpgrade = (): void => {
      if (latestRun === undefined) {
        return;
      }

      api
        .cancelUpgrade(latestRun.id)
        .then((run) => {
          setLatestRun(run);
          toast("upgrade cancelled", "success");
        })
        .catch((error: unknown) => {
          toast(ApiError.describe(error), "error");
        });
    };

    const processFrom = (name: string): void => {
      api
        .createDraft(null, name)
        .then((created) => {
          void navigate(PageUrls.catalog.draft(created.id, { pane: "connections" }));
        })
        .catch((error: unknown) => {
          toast(ApiError.describe(error), "error");
        });
    };

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
        <PageNotices>
          {latestRun !== undefined && (
            <UpgradeBar run={latestRun} canCancel={access.can_edit} onCancel={cancelUpgrade} />
          )}
        </PageNotices>
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
                actions={null}
                processes={processes}
                drafts={drafts}
                currentDraftId={undefined}
                onNewProcess={newProcess}
                onUpgradeAll={access.can_edit && latestRun?.status !== "running" ? upgradeAll : undefined}
                onProcessFrom={access.can_edit ? processFrom : undefined}
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
            <EmptyState fill title="pick a process on the left or press “+” to start one" mark="home-hint" />
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
              void navigate(PageUrls.catalog.draft(created.id));
            }}
            onClose={() => {
              setDialog(null);
            }}
          />
        )}
        {dialog?.kind === "connection" && (
          <ConnectionDialog
            doc={dialog.doc}
            row={null}
            onSaved={(saved) => {
              setDialog(null);
              toast(`connection ${saved.name} saved; sync it to see its tables`, "success");
              void navigate(PageUrls.catalog.connection(saved.id));
            }}
            onClose={() => {
              setDialog(null);
            }}
          />
        )}
      </Page>
    );
  };

  return (
    <Async
      state={state}
      fill
      title={{ loading: "loading the catalog", failed: "the catalog is not available" }}
      render={renderHome}
    />
  );
}

async function load(api: CatalogApi): Promise<Lists> {
  const access = await api.access();
  const processes = await api.processes();
  const drafts = await api.myDrafts();
  const runs = await api.upgradeRuns(undefined, undefined);
  return { access, processes, drafts, latestRun: runs[0] };
}

