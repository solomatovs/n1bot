import { ReactFlowProvider } from "@xyflow/react";
import { Link2, PanelLeft, Pencil, Settings2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import { Canvas } from "../components/canvas/Canvas";
import { CanvasToolbar } from "../components/CanvasToolbar";
import { ConnectionDialog } from "../components/connections/ConnectionDialog";
import { DetailPanel } from "../components/DetailPanel";
import { DraftActions } from "../components/edit/DraftActions";
import { FlowForm } from "../components/edit/FlowForm";
import { NamePrompt } from "../components/edit/NamePrompt";
import { ProcessDialog } from "../components/edit/ProcessDialog";
import { ShareDialog } from "../components/edit/ShareDialog";
import { LeftPane } from "../components/LeftPane";
import { ObjectPanel } from "../components/ObjectPanel";
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
} from "../model/catalog";
import { DraftEditor, type ApplyOutcome } from "../model/editor";
import type { EditActions } from "../model/editing";
import type { GraphOptions, ShowMode } from "../model/graph";
import { blankFlow, blankGroup, blankNode, removeGroupWithNodes, removeNodeWithFlows, type CatalogOp } from "../model/ops";
import { SchemaDoc, parseSchema } from "../model/schema";
import { readUrlState, writeUrlState, type UrlState } from "../model/urlState";
import {
  Alert,
  Button,
  Chip,
  Detail,
  Dialog,
  EmptyState,
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
  useToast,
} from "../ui";

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
};

type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; loaded: Loaded };

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
  const { api } = useServices();
  const toast = useToast();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [params, setParams] = useSearchParams();
  const [paneOpen, setPaneOpen] = useState(() => !narrowScreen());
  const [tidyCount, setTidyCount] = useState(0);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [retargeting, setRetargeting] = useState<string | null>(null);
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
          setState({ status: "ready", loaded: loadedOfDraft(draftState, context, base) });
        })
        .catch((error: unknown) => {
          setState({ status: "failed", message: describe(error) });
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

        if (result.kind === "draft") {
          const base: Base = {
            access: result.access,
            process: result.process,
            currentVersion: result.currentVersion,
            drafts: result.drafts,
            processes: result.processes,
          };
          editor.current = new DraftEditor(api, result.state.draft.id, result.state, (next) => {
            takeDraft(next, base);
          });
          setState({ status: "ready", loaded: loadedOfDraft(result.state, result.context, base) });
          return;
        }

        editor.current = null;
        setState({ status: "ready", loaded: loadedOfPublished(result) });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({ status: "failed", message: describe(error) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, source, takeDraft]);

  useEffect(() => {
    setState({ status: "loading" });
    return reload();
  }, [reload]);

  // живое обновление: чужие порции в этот черновик, новая версия процесса,
  // новая версия снимка (колонки и устаревание), черновики процесса
  useEffect(() => {
    if (guest) {
      return undefined;
    }

    return api.events((message) => {
      if (source.kind === "draft" && message.draft_id === source.draftId) {
        void editor.current?.refresh().catch((error: unknown) => {
          toast(describe(error), "error");
        });
        return;
      }

      lastLoaded.current = "";
      reload();
    });
  }, [api, source, guest, reload, toast]);

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
          toast(describe(error), "error");
        });
        return;
      }

      if (state.status !== "ready" || source.kind !== "published" || !state.loaded.access.can_edit) {
        return;
      }

      const { process, drafts } = state.loaded;
      const mine = drafts.filter((item) => item.process_id === process.id).length;
      // id черновика — из созданного редактора: событие шины о новом черновике
      // может перечитать опубликованный процесс и сбросить editor.current раньше
      // адрес страницы (вкладка панели, режим, скрытые) едет в черновик как есть
      startDraft(process.id, `draft ${mine + 1}`)
        .then((created) => created.apply(ops).then((outcome) => ({ created, outcome })))
        .then(({ created, outcome }) => {
          report(outcome);
          void navigate({ pathname: `/drafts/${created.draftId}`, search: params.toString() });
        })
        .catch((error: unknown) => {
          toast(describe(error), "error");
        });
    },
    [toast, state, source, startDraft, navigate, params],
  );

  // опции холста — одним объектом на состояние адреса: новый объект каждый
  // рендер заставлял бы холст заново строить граф
  const hasDraft = state.status === "ready" && state.loaded.draft !== undefined;
  const hiddenKey = [...url.hidden].sort().join(",");
  const options = useMemo<GraphOptions>(
    () => ({
      showMode: url.showMode,
      showDiff: hasDraft && url.showDiff,
      hidden: new Set(hiddenKey.split(",").filter((id) => id !== "")),
    }),
    [url.showMode, url.showDiff, hiddenKey, hasDraft],
  );
  if (state.status === "loading") {
    return <EmptyState fill title="loading the process" />;
  }

  if (state.status === "failed") {
    return (
      <EmptyState fill title="the process is not available">
        {state.message}
      </EmptyState>
    );
  }

  const { access, process, catalog, draft, drafts, processes, currentVersion, seq } = state.loaded;
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
        toast(describe(error), "error");
      });
  };

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

  const actions = guest ? null : (
    <>
      {draft?.status === "open" && (
        <>
          <DraftActions
            api={api}
            draft={draft}
            currentVersion={currentVersion}
            staleCount={catalog.staleCount}
            onChanged={() => {
              lastLoaded.current = "";
              reload();
            }}
            onPublished={(version) => {
              void navigate(`/processes/${version.process_id}`);
            }}
            onDiscarded={() => {
              if (process.id === "") {
                void navigate("/");
                return;
              }

              void navigate(`/processes/${process.id}`);
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
          {!guest && <TopbarLink to="/">processes</TopbarLink>}
          <TopbarTitle>{process.name}</TopbarTitle>
          <Chip tone="muted" mark="version-chip">
            v{currentVersion}
          </Chip>
          {catalog.staleCount > 0 && (
            <Chip tone="warn" mark="stale-chip">
              {catalog.staleCount} stale
            </Chip>
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
        </PageNotices>
        <PageBody pane={paneOpen} detail={active !== undefined || selectedObject !== undefined}>
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
              void navigate("/");
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
    </ReactFlowProvider>
  );
}

/** Узкий экран: панели становятся ящиками поверх сцены, список по умолчанию закрыт. */
const NARROW_MAX_WIDTH = 900;

function narrowScreen(): boolean {
  return window.matchMedia(`(max-width: ${NARROW_MAX_WIDTH}px)`).matches;
}

type Base = Omit<Loaded, "catalog" | "draft" | "seq">;

function loadedOfDraft(state: DraftState, context: ProcessContext, base: Base): Loaded {
  return {
    ...base,
    catalog: new Catalog(state.snapshot, state.diff, context),
    draft: state.draft,
    seq: state.seq,
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
    }
  | {
      kind: "published";
      access: Access;
      process: Process;
      snapshot: DraftState["snapshot"];
      context: ProcessContext;
      drafts: Draft[];
      processes: Process[];
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
    };
  }

  const access = await api.access();
  const processes = await api.processes();
  if (source.kind === "draft") {
    const state = await api.draft(source.draftId);
    const process = await processOfDraft(api, state.draft);
    const context = await api.draftContext(source.draftId);
    const drafts = await api.myDrafts();
    return {
      kind: "draft",
      access,
      process,
      state,
      context,
      currentVersion: process.latest_version,
      drafts,
      processes,
    };
  }

  const process = await api.process(source.processId);
  const snapshot = await api.snapshot(source.processId);
  const context = await api.context(source.processId);
  const drafts = await api.myDrafts();
  return { kind: "published", access, process, snapshot, context, drafts, processes };
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
  };
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return String(error);
}
