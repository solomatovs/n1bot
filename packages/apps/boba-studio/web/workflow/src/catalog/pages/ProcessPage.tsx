import { ReactFlowProvider } from "@xyflow/react";
import { ArrowUpCircle, Link2, PanelLeft, Pencil, Settings2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api/transport";
import { PageUrls } from "../../config";
import type { CatalogChanged } from "../../model/workflow";
import { Async, type Loadable } from "../../components/Async";
import type { CatalogApi } from "../api/client";
import { useCatalog, useCatalogChanges, type CatalogChangeListener } from "../services";
import { Canvas } from "../components/canvas/Canvas";
import { CanvasToolbar } from "../components/CanvasToolbar";
import { ConnectionDialog } from "../../components/connections/ConnectionsBoard";
import { DetailPanel } from "../components/DetailPanel";
import { DraftActions } from "../components/edit/DraftActions";
import { FlowForm } from "../components/edit/FlowForm";
import { NamePrompt } from "../components/edit/NamePrompt";
import { ProcessDialog } from "../components/edit/ProcessDialog";
import { ShareDialog } from "../components/edit/ShareDialog";
import { LeftPane } from "../components/LeftPane";
import { ObjectPanel } from "../components/ObjectPanel";
import { UpgradeBar } from "../components/tasks/TaskBar";
import {
  Catalog,
  type Access,
  type Draft,
  type DraftState,
  type Flow,
  type Group,
  type Process,
  type ProcessContext,
  type ProcessNode,
  type Upgrade,
  type UpgradeRun,
} from "../model/catalog";
import { DraftEditor, type ApplyOutcome } from "../model/editor";
import type { EditActions } from "../model/editing";
import type { GraphOptions, ShowMode } from "../model/graph";
import { blankFlow, blankGroup, blankNode, removeGroupWithNodes, removeNodeWithFlows, type CatalogOp } from "../model/ops";
import { SchemaDoc, parseSchema } from "../../model/schema";
import { readUrlState, writeUrlState, type UrlState } from "../model/urlState";
import {
  Alert,
  Button,
  Chip,
  Detail,
  Dialog,
  IconButton,
  Page,
  PageBody,
  PageNotices,
  Pane,
  Scene,
  Topbar,
  TopbarHint,
  TopbarLink,
  TopbarSpacer,
  TopbarTitle,
  narrowScreen,
  useToast,
} from "../../ui";

/** Что показывает страница: опубликованный процесс, его черновик либо
 * опубликованный процесс по ссылке для гостя. */
export type PageSource =
  | { kind: "published"; processId: string }
  | { kind: "draft"; draftId: string }
  | { kind: "shared"; token: string };

/** Гость по ссылке: входа нет, прав нет, черновиков и подключений не видно. */
const GUEST: Access = { user_id: "", login: "", can_view: false, can_edit: false };

type Loaded = {
  access: Access;
  process: Process;
  catalog: Catalog;
  currentVersion: number;
  draft: Draft | undefined;
  /** Свои открытые черновики всех процессов: строки плоского списка панели. */
  drafts: Draft[];
  /** Все процессы каталога для панели; гостю по ссылке не видны. */
  processes: Process[];
  /** Номер последней порции черновика; у остальных 0. Виден тестам как data-seq. */
  seq: number;
  /** Привязки отстают от последних версий снимков: есть что поднимать. */
  behind: boolean;
  /** Последний итог upgrade процесса или черновика; без записей — нет. */
  lastUpgrade: Upgrade | undefined;
};


/** Диалоги: имя черновика, имя группы (новой пустой или существующей),
 * форма потока, свойства процесса, ссылки на просмотр, новое подключение;
 * новый процесс заводится только на входе в каталог. */
type DialogState =
  | { kind: "draft-name" }
  | { kind: "group"; group: Group | undefined }
  | { kind: "flow"; flow: Flow; fresh: boolean; pickTarget: boolean }
  | { kind: "process" }
  | { kind: "share" }
  | { kind: "connection"; doc: SchemaDoc };

/** Страница процесса: слои и узлы над объектами подключений, состояние в
 * адресе, три панели; на черновике — правки операциями, публикация и живое
 * обновление по событиям; по ссылке — только чтение. */
export function ProcessPage({ source }: { source: PageSource }): ReactElement {
  const { api, workflowApi } = useCatalog();
  const toast = useToast();
  const [state, setState] = useState<Loadable<Loaded>>({ kind: "loading" });
  const [params, setParams] = useSearchParams();
  const [paneOpen, setPaneOpen] = useState(() => !narrowScreen());
  const [tidyCount, setTidyCount] = useState(0);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [retargeting, setRetargeting] = useState<string | null>(null);
  // запуск upgrade живёт своей полосой: ход приходит событиями по upgrade_id
  const [latestRun, setLatestRun] = useState<UpgradeRun | undefined>(undefined);
  const editor = useRef<DraftEditor | null>(null);
  const navigate = useNavigate();
  const url = useMemo(() => readUrlState(params), [params]);
  const guest = source.kind === "shared";

  const update = useCallback(
    (patch: Partial<UrlState>) => {
      setParams((current) => writeUrlState({ ...readUrlState(current), ...patch }, current), { replace: true });
    },
    [setParams],
  );

  // контекст черновика перечитывается после каждой порции: колонки новых узлов
  const takeDraft = useCallback(
    (draftState: DraftState, base: Base) => {
      api
        .draftContext(draftState.draft.id)
        .then((context) => {
          setState({ kind: "ready", value: loadedOfDraft(draftState, context, base) });
        })
        .catch((error: unknown) => {
          setState({ kind: "error", message: ApiError.describe(error) });
        });
    },
    [api],
  );

  // одинаковый ответ (своё же событие по SSE после правки) не перекладывает граф
  const lastLoaded = useRef("");
  // загрузка по событию шины, начатая до перехода на другой процесс, не
  // дописывает прежний процесс поверх нового
  const shownSource = useRef(source);
  shownSource.current = source;

  const reload = useCallback(() => {
    let cancelled = false;
    load(api, source)
      .then((result) => {
        if (cancelled || shownSource.current !== source) {
          return;
        }

        const key = JSON.stringify(result);
        if (key === lastLoaded.current) {
          return;
        }
        lastLoaded.current = key;

        setLatestRun(result.latestRun);
        if (result.kind === "draft") {
          const base: Base = {
            access: result.access,
            process: result.process,
            currentVersion: result.currentVersion,
            drafts: result.drafts,
            processes: result.processes,
            lastUpgrade: result.lastUpgrade,
          };
          editor.current = new DraftEditor(api, result.state.draft.id, result.state, (next) => {
            takeDraft(next, base);
          });
          setState({ kind: "ready", value: loadedOfDraft(result.state, result.context, base) });
          return;
        }

        editor.current = null;
        setState({ kind: "ready", value: loadedOfPublished(result) });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({ kind: "error", message: ApiError.describe(error) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, source, takeDraft]);

  useEffect(() => {
    setState({ kind: "loading" });
    return reload();
  }, [reload]);

  // живое обновление: чужие порции в этот черновик, новая версия процесса,
  // новая версия снимка (колонки и устаревание), черновики процесса
  const onCatalogChange = useCallback(
    (message: CatalogChanged) => {
      if (source.kind === "draft" && message.draft_id === source.draftId) {
        void editor.current?.refresh().catch((error: unknown) => {
          toast(ApiError.describe(error), "error");
        });
        return;
      }

      // ход запуска upgrade: своя полоса; итог перечитывает страницу
      if (message.upgrade_id !== null) {
        const runId = message.upgrade_id;
        api
          .upgradeRun(runId)
          .then((run) => {
            if (!concernsPage(run, source)) {
              return;
            }

            setLatestRun(run);
            if (run.status !== "running") {
              lastLoaded.current = "";
              reload();
            }
          })
          .catch((error: unknown) => {
            toast(ApiError.describe(error), "error");
          });
        return;
      }

      lastLoaded.current = "";
      reload();
    },
    [api, source, reload, toast],
  );

  let catalogListener: CatalogChangeListener | null = onCatalogChange;
  if (guest) {
    catalogListener = null;
  }
  useCatalogChanges(catalogListener);

  // первая правка опубликованного процесса сама заводит черновик: пока он
  // заводится, следующие порции ждут его и ложатся в него же
  const starting = useRef<Promise<DraftEditor> | null>(null);

  const startDraft = useCallback(
    (processId: string, name: string): Promise<DraftEditor> => {
      starting.current ??= api
        .createDraft(processId, name)
        .then((draft) => api.draft(draft.id))
        .then((state) => {
          const created = new DraftEditor(api, state.draft.id, state, () => undefined);
          editor.current = created;
          return created;
        });

      return starting.current;
    },
    [api],
  );

  const apply = useCallback(
    (ops: CatalogOp[]) => {
      const report = (outcome: ApplyOutcome): void => {
        if (outcome.kind === "rejected") {
          toast(outcome.reason, "error");
        }
      };

      const current = editor.current;
      if (current !== null) {
        current.apply(ops).then(report).catch((error: unknown) => {
          toast(ApiError.describe(error), "error");
        });
        return;
      }

      if (state.kind !== "ready" || source.kind !== "published" || !state.value.access.can_edit) {
        return;
      }

      const { process, drafts } = state.value;
      const mine = drafts.filter((item) => item.process_id === process.id).length;
      // id черновика — из созданного редактора: событие шины о новом черновике
      // может перечитать опубликованный процесс и сбросить editor.current раньше
      // адрес страницы (вкладка панели, режим, скрытые) едет в черновик как есть
      startDraft(process.id, `draft ${mine + 1}`)
        .then((created) => created.apply(ops).then((outcome) => ({ created, outcome })))
        .then(({ created, outcome }) => {
          report(outcome);
          void navigate({ pathname: PageUrls.catalog.draft(created.draftId), search: params.toString() });
        })
        .catch((error: unknown) => {
          toast(ApiError.describe(error), "error");
        });
    },
    [toast, state, source, startDraft, navigate, params],
  );

  // опции холста — одним объектом на состояние адреса: новый объект каждый
  // рендер заставлял бы холст заново строить граф
  const hasDraft = state.kind === "ready" && state.value.draft !== undefined;
  const hiddenKey = [...url.hidden].sort().join(",");
  const options = useMemo<GraphOptions>(
    () => ({
      showMode: url.showMode,
      showDiff: hasDraft && url.showDiff,
      hidden: new Set(hiddenKey.split(",").filter((id) => id !== "")),
    }),
    [url.showMode, url.showDiff, hiddenKey, hasDraft],
  );

  const renderProcess = (loaded: Loaded): ReactElement => {
    const { access, process, catalog, draft, drafts, processes, currentVersion, seq, behind, lastUpgrade } = loaded;
    // черновик правится; опубликованный процесс тоже, первая правка заводит черновик
    const editable = draft?.status === "open" || (source.kind === "published" && access.can_edit);
    const owned = access.can_edit && process.owner_id === access.user_id;
    const active = url.active === undefined ? undefined : catalog.node(url.active);
    const selectedObject = active === undefined ? url.object : undefined;
    const retargetFor = retargeting === null ? undefined : catalog.node(retargeting);

    const editing: EditActions | undefined = editable
      ? {
          apply,
          newGroup: () => {
            setDialog({ kind: "group", group: undefined });
          },
          renameGroup: (group) => {
            setDialog({ kind: "group", group });
          },
          removeGroup: (group) => {
            apply(removeGroupWithNodes(group.id, catalog.nodesOf(group.id)));
          },
          addNode: (ref, position, groupId) => {
            // брошенная таблица встаёт молча: панель узла не открывается, чтобы
            // не сжимать сцену, пока пользователь накидывает таблицы
            const node = { ...blankNode(ref, position), group_id: groupId };
            apply([{ op: "add_node", node }]);
            update({ object: undefined });
          },
          moveNodes: (moves) => {
            const ops: CatalogOp[] = [];
            for (const move of moves) {
              ops.push({ op: "set_node", node: { ...move.node, position: move.position, group_id: move.groupId } });
            }
            apply(ops);
          },
          resizeNode: (resize) => {
            apply([{ op: "set_node", node: { ...resize.node, position: resize.position, width: resize.width } }]);
          },
          removeNode: (node: ProcessNode) => {
            const flows = catalog.flowsOf(node.id);
            apply(removeNodeWithFlows(node.id, [...flows.incoming, ...flows.outgoing]));
            update({ active: undefined });
          },
          retargetNode: (node, ref) => {
            apply([{ op: "retarget_node", id: node.id, ref }]);
            setRetargeting(null);
            update({ active: node.id, object: undefined });
          },
          newFlow: (from: ProcessNode) => {
            setDialog({ kind: "flow", flow: blankFlow(from.id, ""), fresh: true, pickTarget: true });
          },
          editFlow: (flow: Flow) => {
            setDialog({ kind: "flow", flow, fresh: false, pickTarget: false });
          },
          connect: (connection) => {
            const existing = catalog.flowBetween(connection.from, connection.to);
            if (connection.fromColumn === undefined || connection.toColumn === undefined) {
              const flow = existing ?? blankFlow(connection.from, connection.to);
              setDialog({ kind: "flow", flow, fresh: existing === undefined, pickTarget: false });
              return;
            }

            const pair = { from_column: connection.fromColumn, to_column: connection.toColumn };
            if (existing === undefined) {
              apply([{ op: "add_flow", flow: { ...blankFlow(connection.from, connection.to), columns: [pair] } }]);
              return;
            }

            const repeated = existing.columns.some(
              (link) => link.from_column === pair.from_column && link.to_column === pair.to_column,
            );
            if (repeated) {
              toast(`${pair.from_column} → ${pair.to_column} is already in the flow`, "error");
              return;
            }

            apply([{ op: "set_flow", flow: { ...existing, columns: [...existing.columns, pair] } }]);
          },
          removeLinks: (removed) => {
            const ops: CatalogOp[] = [];
            const byFlow = new Map<string, Set<string>>();
            for (const item of removed) {
              const keys = byFlow.get(item.flowId) ?? new Set<string>();
              if (item.pair === undefined) {
                keys.add("*");
              } else {
                keys.add(`${item.pair.from_column}->${item.pair.to_column}`);
              }
              byFlow.set(item.flowId, keys);
            }

            for (const [flowId, keys] of byFlow) {
              const flow = catalog.flows.find((item) => item.id === flowId);
              if (flow === undefined) {
                continue;
              }

              const kept = flow.columns.filter((link) => !keys.has(`${link.from_column}->${link.to_column}`));
              if (keys.has("*") || kept.length === 0) {
                ops.push({ op: "remove_flow", id: flow.id });
              } else {
                ops.push({ op: "set_flow", flow: { ...flow, columns: kept } });
              }
            }

            if (ops.length > 0) {
              apply(ops);
            }
          },
        }
      : undefined;

    // upgrade — задача, как синхронизация: запуск возвращается сразу, ход и
    // итог приходят событиями; blocked — привязки прежние, проблемы видны
    // пометками stale
    const upgrade = (): void => {
      let request: () => Promise<UpgradeRun> = () => api.upgradeProcess(process.id);
      if (draft !== undefined) {
        const draftId = draft.id;
        request = () => api.upgradeDraft(draftId);
      }

      request()
        .then((run) => {
          setLatestRun(run);
          toast("upgrade started", "success");
        })
        .catch((error: unknown) => {
          toast(ApiError.describe(error), "error");
        });
    };

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

    let upgradable = access.can_edit && behind;
    if (draft !== undefined && draft.status !== "open") {
      upgradable = false;
    }
    if (latestRun?.status === "running") {
      upgradable = false;
    }
    const blocked = lastUpgrade?.status === "blocked" && behind;

    const renameDraft = (name: string): void => {
      if (draft === undefined) {
        return;
      }

      api
        .renameDraft(draft.id, name)
        .then(() => {
          setDialog(null);
          lastLoaded.current = "";
          reload();
        })
        .catch((error: unknown) => {
          toast(ApiError.describe(error), "error");
        });
    };

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

    const actions = guest ? null : (
      <>
        {draft?.status === "open" && (
          <>
            <DraftActions
              api={api}
              draft={draft}
              currentVersion={currentVersion}
              onChanged={() => {
                lastLoaded.current = "";
                reload();
              }}
              onPublished={(version) => {
                void navigate(PageUrls.catalog.process(version.process_id));
              }}
              onDiscarded={() => {
                if (process.id === "") {
                  void navigate(PageUrls.catalog.home());
                  return;
                }

                void navigate(PageUrls.catalog.process(process.id));
              }}
            />
            <IconButton
              size="sm"
              ghost
              aria-label="rename draft"
              onClick={() => {
                setDialog({ kind: "draft-name" });
              }}
            >
              <Pencil size={14} />
            </IconButton>
          </>
        )}
        {source.kind === "published" && owned && (
          <Button
            size="sm"
            tone="ghost"
            icon={Link2}
            onClick={() => {
              setDialog({ kind: "share" });
            }}
            data-testid="share-button"
          >
            share
          </Button>
        )}
        {source.kind === "published" && access.can_edit && (
          <IconButton
            size="sm"
            ghost
            aria-label="process settings"
            onClick={() => {
              setDialog({ kind: "process" });
            }}
          >
            <Settings2 size={14} />
          </IconButton>
        )}
      </>
    );


    return (
      <ReactFlowProvider>
        <Page
          mark="catalog-page"
          data-source={source.kind}
          data-process={process.name}
          data-editable={editable}
          data-owned={owned}
          data-seq={seq}
          data-stale={catalog.staleCount}
        >
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
            {!guest && <TopbarLink to={PageUrls.catalog.home()}>processes</TopbarLink>}
            <TopbarTitle>{process.name}</TopbarTitle>
            <Chip tone="muted" mark="version-chip">
              v{currentVersion}
            </Chip>
            {catalog.staleCount > 0 && (
              <Chip tone="warn" mark="stale-chip">
                {catalog.staleCount} stale
              </Chip>
            )}
            {upgradable && (
              <Button size="sm" tone="signal" icon={ArrowUpCircle} onClick={upgrade} data-testid="upgrade-button">
                upgrade
              </Button>
            )}
            {draft !== undefined && (
              <Button
                size="sm"
                tone={url.showDiff ? "signal" : "ghost"}
                aria-pressed={url.showDiff}
                onClick={() => {
                  update({ showDiff: !url.showDiff });
                }}
              >
                diff
              </Button>
            )}
            <TopbarSpacer />
            <TopbarHint>
              {catalog.nodes.length} nodes · {catalog.flows.length} flows
            </TopbarHint>
          </Topbar>
          <PageNotices>
            {draft?.status === "open" && (
              <Alert tone="draft" mark="draft-bar">
                <span data-testid="draft-name">draft “{draft.name}”</span> · seq {seq} · over v{draft.base_version} ·
                publish it to make the changes part of the process
              </Alert>
            )}
            {draft !== undefined && draft.status !== "open" && (
              <Alert tone="info" mark="draft-closed">
                This draft is {draft.status}; it is read-only now.
              </Alert>
            )}
            {guest && (
              <Alert tone="info" mark="shared-bar">
                Shared view of the published process; read-only.
              </Alert>
            )}
            {latestRun !== undefined && (
              <UpgradeBar run={latestRun} canCancel={access.can_edit} onCancel={cancelUpgrade} />
            )}
            {blocked && (
              <Alert tone="error" mark="upgrade-blocked">
                <span data-testid="upgrade-problems">{lastUpgrade.problems.length} problem(s)</span> keep it on the old
                snapshots: fix the stale nodes and flows, then upgrade again.
              </Alert>
            )}
          </PageNotices>
          <PageBody>
            {paneOpen && (
              <Pane>
                <LeftPane
                  api={api}
                  guest={guest}
                  tab={url.pane}
                  onTab={(tab) => {
                    update({ pane: tab });
                  }}
                  actions={actions}
                  processes={processes}
                  drafts={drafts}
                  currentDraftId={draft?.id}
                  onNewProcess={undefined}
                  onUpgradeAll={access.can_edit ? upgradeAll : undefined}
                  onProcessFrom={access.can_edit ? processFrom : undefined}
                  open={{
                    processId: process.id,
                    catalog,
                    activeId: url.active,
                    hidden: url.hidden,
                    showDiff: options.showDiff,
                    editing,
                    onActivate: (id) => {
                      update({ active: id, object: undefined });
                    },
                    onToggleHidden: (id) => {
                      const hidden = new Set(url.hidden);
                      if (hidden.has(id)) {
                        hidden.delete(id);
                      } else {
                        hidden.add(id);
                      }
                      update({ hidden });
                    },
                  }}
                  onAddConnection={access.can_edit ? openConnectionDialog : undefined}
                  selectedObject={selectedObject}
                  onSelectObject={(ref) => {
                    update({ active: undefined, object: ref });
                  }}
                />
              </Pane>
            )}
            <Scene>
              <>
                  <CanvasToolbar
                    showMode={url.showMode}
                    onShowMode={(mode: ShowMode) => {
                      update({ showMode: mode });
                    }}
                    onTidy={() => {
                      setTidyCount((count) => count + 1);
                    }}
                    onGroup={editing?.newGroup}
                  />
                  <Canvas
                    catalog={catalog}
                    options={options}
                    activeId={url.active}
                    tidyCount={tidyCount}
                    persistTidy={draft?.status === "open"}
                    onActivate={(id) => {
                      update({ active: id, object: undefined });
                    }}
                    onFlowOpen={
                      editable
                        ? (flowId) => {
                            const flow = catalog.flows.find((item) => item.id === flowId);
                            if (flow !== undefined) {
                              setDialog({ kind: "flow", flow, fresh: false, pickTarget: false });
                            }
                          }
                        : undefined
                    }
                    editing={editing}
                  />
                </>
            </Scene>
            {active !== undefined && (
              <Detail>
                <DetailPanel
                  key={active.id}
                  api={api}
                  catalog={catalog}
                  node={active}
                  cardSource={source.kind === "shared" ? { kind: "shared", token: source.token } : { kind: "pinned" }}
                  showDiff={options.showDiff}
                  editing={editing}
                  retargeting={retargeting === active.id}
                  onRetargetToggle={() => {
                    setRetargeting((current) => (current === active.id ? null : active.id));
                    update({ pane: "connections" });
                  }}
                  onActivate={(id) => {
                    update({ active: id, object: undefined });
                  }}
                  onOpenObject={
                    guest
                      ? undefined
                      : (ref) => {
                          update({ active: undefined, object: ref });
                        }
                  }
                  onClose={() => {
                    update({ active: undefined });
                  }}
                />
              </Detail>
            )}
            {active === undefined && selectedObject !== undefined && (
              <Detail>
                <ObjectPanel
                  key={`${selectedObject.connection_id}:${selectedObject.kind}:${selectedObject.path.join("/")}`}
                  api={api}
                  catalog={catalog}
                  object={selectedObject}
                  editing={editing}
                  retargetFor={retargetFor}
                  onOpenNode={(id) => {
                    update({ active: id, object: undefined });
                  }}
                  onOpenObject={(ref) => {
                    update({ active: undefined, object: ref });
                  }}
                  onClose={() => {
                    update({ object: undefined });
                  }}
                />
              </Detail>
            )}
          </PageBody>
          {dialog?.kind === "draft-name" && (
            <NamePrompt
              title="rename draft"
              mark="draft-name"
              label="draft name"
              initial={draft?.name ?? ""}
              onSubmit={renameDraft}
              onClose={() => {
                setDialog(null);
              }}
            />
          )}
          {dialog?.kind === "group" && (
            <NamePrompt
              title={dialog.group === undefined ? "new group" : "rename group"}
              mark="group-name"
              label="group name"
              initial={dialog.group?.name ?? ""}
              onSubmit={(name) => {
                const group = dialog.group;
                if (group === undefined) {
                  apply([{ op: "add_group", group: blankGroup(name) }]);
                } else {
                  apply([{ op: "set_group", group: { ...group, name } }]);
                }
                setDialog(null);
              }}
              onClose={() => {
                setDialog(null);
              }}
            />
          )}
          {dialog?.kind === "flow" && (
            <Dialog
              title={dialog.fresh ? "new flow" : "flow"}
              mark="flow"
              onClose={() => {
                setDialog(null);
              }}
            >
              <FlowForm
                catalog={catalog}
                flow={dialog.flow}
                pickTarget={dialog.pickTarget}
                onSave={(flow) => {
                  const op: CatalogOp = dialog.fresh ? { op: "add_flow", flow } : { op: "set_flow", flow };
                  apply([op]);
                  setDialog(null);
                }}
                onCancel={() => {
                  setDialog(null);
                }}
                onDelete={
                  dialog.fresh
                    ? undefined
                    : () => {
                        apply([{ op: "remove_flow", id: dialog.flow.id }]);
                        setDialog(null);
                      }
                }
              />
            </Dialog>
          )}
          {dialog?.kind === "process" && (
            <ProcessDialog
              api={api}
              process={process}
              owned={owned}
              onSaved={() => {
                setDialog(null);
                lastLoaded.current = "";
                reload();
              }}
              onDeleted={() => {
                void navigate(PageUrls.catalog.home());
              }}
              onClose={() => {
                setDialog(null);
              }}
            />
          )}
          {dialog?.kind === "share" && (
            <ShareDialog
              api={api}
              process={process}
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
      </ReactFlowProvider>
    );
  };

  return (
    <Async
      state={state}
      fill
      title={{ loading: "loading the process", failed: "the process is not available" }}
      render={renderProcess}
    />
  );
}

type Base = Omit<Loaded, "catalog" | "draft" | "seq" | "behind">;

/** Отставание и строка черновика в списке берутся из самого черновика:
 * состояние приходит и по событию шины, когда привязки уже подняты, а список
 * панели при этом не перечитывается. */
function loadedOfDraft(state: DraftState, context: ProcessContext, base: Base): Loaded {
  const drafts: Draft[] = [];
  for (const draft of base.drafts) {
    if (draft.id === state.draft.id) {
      drafts.push(state.draft);
    } else {
      drafts.push(draft);
    }
  }

  return {
    ...base,
    drafts,
    catalog: new Catalog(state.snapshot, state.diff, context),
    draft: state.draft,
    seq: state.seq,
    behind: state.draft.behind,
  };
}

type LoadResult =
  | {
      kind: "draft";
      access: Access;
      process: Process;
      state: DraftState;
      context: ProcessContext;
      currentVersion: number;
      drafts: Draft[];
      processes: Process[];
      lastUpgrade: Upgrade | undefined;
      latestRun: UpgradeRun | undefined;
    }
  | {
      kind: "published";
      access: Access;
      process: Process;
      snapshot: DraftState["snapshot"];
      context: ProcessContext;
      drafts: Draft[];
      processes: Process[];
      lastUpgrade: Upgrade | undefined;
      latestRun: UpgradeRun | undefined;
    };

async function load(api: CatalogApi, source: PageSource): Promise<LoadResult> {
  if (source.kind === "shared") {
    const shared = await api.shared(source.token);
    return {
      kind: "published",
      access: GUEST,
      process: shared.process,
      snapshot: shared.snapshot,
      context: shared.context,
      drafts: [],
      processes: [],
      lastUpgrade: undefined,
      latestRun: undefined,
    };
  }

  const access = await api.access();
  const processes = await api.processes();
  if (source.kind === "draft") {
    const state = await api.draft(source.draftId);
    const process = await processOfDraft(api, state.draft);
    const context = await api.draftContext(source.draftId);
    const drafts = await api.myDrafts();
    const lastUpgrade = await api.lastDraftUpgrade(source.draftId);
    const runs = await api.upgradeRuns(undefined, source.draftId);
    return {
      kind: "draft",
      access,
      process,
      state,
      context,
      currentVersion: process.latest_version,
      drafts,
      processes,
      lastUpgrade,
      latestRun: runs[0],
    };
  }

  const process = await api.process(source.processId);
  const snapshot = await api.snapshot(source.processId);
  const context = await api.context(source.processId);
  const drafts = await api.myDrafts();
  const lastUpgrade = await api.lastUpgrade(source.processId);
  const runs = await api.upgradeRuns(source.processId, undefined);
  return {
    kind: "published",
    access,
    process,
    snapshot,
    context,
    drafts,
    processes,
    lastUpgrade,
    latestRun: runs[0],
  };
}

/** Процесс черновика; у черновика нового процесса его ещё нет — процесс
 * рисуется по черновику: имя, автор, нулевая версия. */
async function processOfDraft(api: CatalogApi, draft: Draft): Promise<Process> {
  if (draft.process_id !== null) {
    return api.process(draft.process_id);
  }

  return {
    id: "",
    name: draft.name,
    description: "",
    owner_id: draft.created_by,
    created_at: draft.created_at,
    latest_version: 0,
    nodes: 0,
    open_drafts: 1,
    pins: {},
    connections: [],
    behind: false,
    attention: 0,
  };
}

function loadedOfPublished(result: Extract<LoadResult, { kind: "published" }>): Loaded {
  return {
    access: result.access,
    process: result.process,
    catalog: new Catalog(result.snapshot, undefined, result.context),
    currentVersion: result.process.latest_version,
    draft: undefined,
    drafts: result.drafts,
    processes: result.processes,
    seq: 0,
    behind: result.process.behind,
    lastUpgrade: result.lastUpgrade,
  };
}

/** Запуск касается страницы: по всем процессам, по этому процессу или по
 * этому черновику. */
function concernsPage(run: UpgradeRun, source: PageSource): boolean {
  if (run.target === "all") {
    return true;
  }

  if (source.kind === "draft") {
    return run.draft_id === source.draftId;
  }

  if (source.kind === "published") {
    return run.process_id === source.processId;
  }

  return false;
}

