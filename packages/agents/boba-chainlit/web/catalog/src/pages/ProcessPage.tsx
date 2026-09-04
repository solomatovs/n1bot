import { ReactFlowProvider } from "@xyflow/react";
import { Layers, PanelLeft, Pencil, Workflow } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import { Canvas } from "../components/canvas/Canvas";
import { CanvasToolbar } from "../components/CanvasToolbar";
import { DetailPanel } from "../components/DetailPanel";
import { Dialog } from "../components/edit/Dialog";
import { DiagramsDialog } from "../components/edit/DiagramsDialog";
import { DraftActions } from "../components/edit/DraftActions";
import { DraftsDialog } from "../components/edit/DraftsDialog";
import { FlowForm } from "../components/edit/FlowForm";
import { LoadKindsDialog } from "../components/edit/LoadKindsDialog";
import { NamePrompt } from "../components/edit/NamePrompt";
import { ViewActions } from "../components/edit/ViewActions";
import { LeftPane } from "../components/LeftPane";
import { ObjectPanel } from "../components/ObjectPanel";
import { LayoutSaver } from "../model/layoutSaver";
import {
  Catalog,
  type Access,
  type Draft,
  type DraftState,
  type Flow,
  type Layer,
  type NodePosition,
  type ObjectRef,
  type ProcessContext,
  type ProcessNode,
  type View,
  type ViewState,
} from "../model/catalog";
import { DraftEditor } from "../model/editor";
import type { EditActions } from "../model/editing";
import { nodesInView, type GraphOptions, type ShowMode } from "../model/graph";
import { blankFlow, blankLayer, blankNode, removeNodeWithFlows, type CatalogOp } from "../model/ops";
import { readUrlState, writeUrlState, type UrlState } from "../model/urlState";
import { Alert, Button, Chip, EmptyState, IconButton, Select, useToast } from "../ui";

/** Что показывает страница: опубликованный процесс, его срез через диаграмму
 * либо черновик. */
export type PageSource = { kind: "published" } | { kind: "view"; viewId: string } | { kind: "draft"; draftId: string };

type Loaded = {
  access: Access;
  catalog: Catalog;
  title: string;
  version: string;
  currentVersion: number;
  draft: Draft | undefined;
  /** Открытые черновики: вход в правки с опубликованной страницы. */
  drafts: Draft[];
  view: View | undefined;
  saved: NodePosition[];
  /** Номер последней порции черновика; у остальных 0. Виден тестам как data-seq. */
  seq: number;
  /** Вид принадлежит пользователю с правом правок: фильтр, шаринг, раскладка. */
  owned: boolean;
};

type LoadState =
  | { status: "loading" }
  | { status: "failed"; message: string }
  | { status: "denied"; access: Access; views: View[] }
  | { status: "ready"; loaded: Loaded };

/** Диалоги: имя слоя, слой для брошенного объекта, форма потока, виды загрузки,
 * диаграммы, вход в правки. */
type DialogState =
  | { kind: "layer"; layer: Layer | undefined }
  | { kind: "drop"; ref: ObjectRef }
  | { kind: "flow"; flow: Flow; fresh: boolean; pickTarget: boolean }
  | { kind: "load-kinds" }
  | { kind: "diagrams" }
  | { kind: "drafts" };

/** Страница процесса: слои и узлы над объектами источников, состояние в
 * адресе, три панели; на черновике — правки операциями, публикация и живое
 * обновление по событиям; на диаграмме — срез и сохранённая раскладка. */
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
  const [layoutSaves, setLayoutSaves] = useState(0);
  const saver = useMemo(
    () =>
      new LayoutSaver(api, {
        onSaved: () => {
          setLayoutSaves((count) => count + 1);
        },
        onFailed: (message) => {
          toast(message, "error");
        },
      }),
    [api, toast],
  );
  useEffect(
    () => () => {
      saver.dispose();
    },
    [saver],
  );
  const url = useMemo(() => readUrlState(params), [params]);

  const update = useCallback(
    (patch: Partial<UrlState>) => {
      setParams((current) => writeUrlState({ ...readUrlState(current), ...patch }, current), { replace: true });
    },
    [setParams],
  );

  // контекст черновика перечитывается после каждой порции: колонки новых узлов
  const takeDraft = useCallback(
    (draftState: DraftState, base: Omit<Loaded, "catalog" | "title" | "version" | "draft" | "seq">) => {
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

  const reload = useCallback(() => {
    let cancelled = false;
    load(api, source)
      .then((result) => {
        if (cancelled) {
          return;
        }

        const key = JSON.stringify(result);
        if (key === lastLoaded.current) {
          return;
        }
        lastLoaded.current = key;

        if (result.kind === "draft") {
          const base = {
            access: result.access,
            currentVersion: result.currentVersion,
            drafts: [],
            view: undefined,
            saved: [],
            owned: false,
          };
          editor.current = new DraftEditor(api, result.state.draft.id, result.state, (next) => {
            takeDraft(next, base);
          });
          setState({ status: "ready", loaded: loadedOfDraft(result.state, result.context, base) });
          return;
        }

        editor.current = null;
        if (result.kind === "denied") {
          setState({ status: "denied", access: result.access, views: result.views });
          return;
        }

        if (result.kind === "view") {
          setState({ status: "ready", loaded: loadedOfView(result.access, result.state, result.context) });
          return;
        }

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

  // живое обновление: чужие порции в этот черновик, новая версия, правка вида,
  // новая версия источника (колонки и устаревание)
  useEffect(() => {
    return api.events((message) => {
      if (source.kind === "draft" && message.draft_id === source.draftId) {
        void editor.current?.refresh().catch((error: unknown) => {
          toast(describe(error), "error");
        });
        return;
      }

      if (message.version !== null || message.source_id !== null) {
        lastLoaded.current = "";
        reload();
        return;
      }

      if (source.kind === "published" && message.draft_id !== null) {
        lastLoaded.current = "";
        reload();
        return;
      }

      if (source.kind === "view" && message.view_id === source.viewId) {
        reload();
      }
    });
  }, [api, source, reload, toast]);

  const apply = useCallback(
    (ops: CatalogOp[]) => {
      const current = editor.current;
      if (current === null) {
        return;
      }

      current
        .apply(ops)
        .then((outcome) => {
          if (outcome.kind === "rejected") {
            toast(outcome.reason, "error");
          }
        })
        .catch((error: unknown) => {
          toast(describe(error), "error");
        });
    },
    [toast],
  );

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

  if (state.status === "denied") {
    return <SharedOnly access={state.access} views={state.views} />;
  }

  const { access, catalog, draft, drafts, view, saved, currentVersion, seq, owned } = state.loaded;
  const editable = draft?.status === "open";
  const options: GraphOptions = {
    showMode: url.showMode,
    showDiff: draft !== undefined && url.showDiff,
    nodeIds: new Set(view?.node_ids ?? []),
    layerIds: new Set(view?.layer_ids ?? []),
    hidden: url.hidden,
  };
  const nodes = nodesInView(catalog, options);
  const active = url.active === undefined ? undefined : catalog.node(url.active);
  const selectedObject = active === undefined ? url.object : undefined;
  const retargetFor = retargeting === null ? undefined : catalog.node(retargeting);

  const editing: EditActions | undefined = editable
    ? {
        apply,
        addLayer: () => {
          setDialog({ kind: "layer", layer: undefined });
        },
        renameLayer: (layer) => {
          setDialog({ kind: "layer", layer });
        },
        removeLayer: (layer) => {
          apply([{ op: "remove_layer", id: layer.id }]);
        },
        addNode: (layerId, ref) => {
          const node = blankNode(layerId, ref);
          apply([{ op: "add_node", node }]);
          update({ active: node.id, object: undefined });
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
      }
    : undefined;

  const openDrafts = (): void => {
    setDialog({ kind: "drafts" });
  };

  const createDraft = (name: string): void => {
    api
      .createDraft(name)
      .then((created) => {
        void navigate(`/drafts/${created.id}`);
      })
      .catch((error: unknown) => {
        toast(describe(error), "error");
      });
  };

  const empty = catalog.layers.length === 0 && catalog.nodes.length === 0;

  return (
    <ReactFlowProvider>
      <div
        className="page"
        data-testid="catalog-page"
        data-source={source.kind}
        data-editable={editable}
        data-owned={owned}
        data-seq={seq}
        data-layout-saves={layoutSaves}
        data-stale={catalog.staleCount}
      >
        <header className="topbar">
          <IconButton
            aria-label={paneOpen ? "hide the left pane" : "show the left pane"}
            aria-pressed={paneOpen}
            onClick={() => {
              setPaneOpen((open) => !open);
            }}
          >
            <PanelLeft size={16} />
          </IconButton>
          <Link to="/" className="topbar__home">
            catalog
          </Link>
          <span className="topbar__title" data-testid="page-title">
            {state.loaded.title}
          </span>
          <Chip tone="muted">{state.loaded.version}</Chip>
          {catalog.staleCount > 0 && (
            <Chip tone="draft" mark="stale-chip">
              {catalog.staleCount} stale
            </Chip>
          )}
          {draft !== undefined && (
            <Button
              size="tiny"
              tone={url.showDiff ? "signal" : "ghost"}
              aria-pressed={url.showDiff}
              onClick={() => {
                update({ showDiff: !url.showDiff });
              }}
            >
              diff
            </Button>
          )}
          {source.kind === "published" && access.can_view && (
            <Button size="tiny" tone="primary" icon={Pencil} onClick={openDrafts} data-testid="edit-button">
              edit{drafts.length > 0 ? ` · ${drafts.length}` : ""}
            </Button>
          )}
          {draft?.status === "open" && (
            <DraftActions
              api={api}
              draft={draft}
              currentVersion={currentVersion}
              staleCount={catalog.staleCount}
              onChanged={() => {
                lastLoaded.current = "";
                reload();
              }}
              onDiscarded={() => {
                void navigate("/");
              }}
            />
          )}
          {view !== undefined && owned && (
            <ViewActions
              api={api}
              view={view}
              onChanged={reload}
              onDeleted={() => {
                void navigate("/");
              }}
            />
          )}
          <Button
            size="tiny"
            tone="ghost"
            icon={Layers}
            data-testid="load-kinds-button"
            onClick={() => {
              setDialog({ kind: "load-kinds" });
            }}
          >
            load kinds
          </Button>
          <Button
            size="tiny"
            tone="ghost"
            icon={Workflow}
            data-testid="diagrams-button"
            onClick={() => {
              setDialog({ kind: "diagrams" });
            }}
          >
            diagrams
          </Button>
          <span className="topbar__spacer" />
          <span className="topbar__hint mono">
            {nodes.length} nodes · {catalog.flows.length} flows
          </span>
        </header>
        {draft !== undefined && draft.status !== "open" && (
          <Alert tone="info" mark="draft-closed">
            This draft is {draft.status}; it is read-only now.
          </Alert>
        )}
        <div className="page__body" data-pane={paneOpen} data-detail={active !== undefined || selectedObject !== undefined}>
          {paneOpen && (
            <aside className="page__pane">
              <LeftPane
                api={api}
                catalog={catalog}
                nodes={nodes}
                tab={url.pane}
                onTab={(tab) => {
                  update({ pane: tab });
                }}
                activeId={url.active}
                selectedObject={selectedObject}
                hidden={url.hidden}
                showDiff={options.showDiff}
                editing={editing}
                onActivate={(id) => {
                  update({ active: id, object: undefined });
                }}
                onSelectObject={(ref) => {
                  update({ active: undefined, object: ref });
                }}
                onToggleHidden={(id) => {
                  const hidden = new Set(url.hidden);
                  if (hidden.has(id)) {
                    hidden.delete(id);
                  } else {
                    hidden.add(id);
                  }
                  update({ hidden });
                }}
              />
            </aside>
          )}
          <main className="page__scene">
            {empty && (
              <EmptyState fill title="the process is empty">
                <p>
                  Pull the structure of your databases in as metadata sources, then put their objects into layers.
                </p>
                <div className="form__actions" data-testid="empty-actions">
                  <Button
                    size="tiny"
                    tone="primary"
                    onClick={() => {
                      void navigate("/sources");
                    }}
                  >
                    add a source
                  </Button>
                  <Button
                    size="tiny"
                    tone="ghost"
                    onClick={() => {
                      void navigate("/sources?manual=1");
                    }}
                  >
                    manual source
                  </Button>
                  {editable && (
                    <Button size="tiny" onClick={editing?.addLayer}>
                      add a layer
                    </Button>
                  )}
                </div>
              </EmptyState>
            )}
            {!empty && (
              <>
                <CanvasToolbar
                  showMode={url.showMode}
                  onShowMode={(mode: ShowMode) => {
                    update({ showMode: mode });
                  }}
                  onTidy={() => {
                    setTidyCount((count) => count + 1);
                  }}
                />
                <Canvas
                  catalog={catalog}
                  options={options}
                  saved={saved}
                  activeId={url.active}
                  tidyCount={tidyCount}
                  onActivate={(id) => {
                    update({ active: id, object: undefined });
                  }}
                  onConnect={
                    editable
                      ? (from, to) => {
                          setDialog({ kind: "flow", flow: blankFlow(from, to), fresh: true, pickTarget: false });
                        }
                      : undefined
                  }
                  onFlowClick={
                    editable
                      ? (flowId) => {
                          const flow = catalog.flows.find((item) => item.id === flowId);
                          if (flow !== undefined) {
                            setDialog({ kind: "flow", flow, fresh: false, pickTarget: false });
                          }
                        }
                      : undefined
                  }
                  onDrop={
                    editing !== undefined
                      ? (ref, layerId) => {
                          if (catalog.nodeOf(ref) !== undefined) {
                            toast(`${ref.path.join("/")} is already in the process`, "error");
                            return;
                          }

                          if (layerId === undefined) {
                            setDialog({ kind: "drop", ref });
                            return;
                          }

                          editing.addNode(layerId, ref);
                        }
                      : undefined
                  }
                  onMoved={
                    view !== undefined && owned
                      ? (positions) => {
                          saver.schedule(view.id, positions);
                        }
                      : undefined
                  }
                />
              </>
            )}
          </main>
          {active !== undefined && (
            <aside className="page__detail">
              <DetailPanel
                key={active.id}
                api={api}
                catalog={catalog}
                node={active}
                cardSource={source.kind === "view" ? { kind: "view", viewId: source.viewId } : { kind: "pinned" }}
                showDiff={options.showDiff}
                editing={editing}
                retargeting={retargeting === active.id}
                onRetargetToggle={() => {
                  setRetargeting((current) => (current === active.id ? null : active.id));
                  update({ pane: "sources" });
                }}
                onActivate={(id) => {
                  update({ active: id, object: undefined });
                }}
                onClose={() => {
                  update({ active: undefined });
                }}
              />
            </aside>
          )}
          {active === undefined && selectedObject !== undefined && (
            <aside className="page__detail">
              <ObjectPanel
                key={`${selectedObject.source_id}:${selectedObject.kind}:${selectedObject.path.join("/")}`}
                api={api}
                catalog={catalog}
                object={selectedObject}
                editing={editing}
                retargetFor={retargetFor}
                onOpenNode={(id) => {
                  update({ active: id, object: undefined });
                }}
                onClose={() => {
                  update({ object: undefined });
                }}
              />
            </aside>
          )}
        </div>
        {dialog?.kind === "layer" && (
          <NamePrompt
            title={dialog.layer === undefined ? "new layer" : "rename layer"}
            mark="layer-name"
            label="layer name"
            initial={dialog.layer?.name ?? ""}
            onSubmit={(name) => {
              const layer = dialog.layer;
              if (layer === undefined) {
                apply([{ op: "add_layer", layer: blankLayer(name, catalog.nextLayerPosition()) }]);
              } else {
                apply([{ op: "set_layer", layer: { ...layer, name } }]);
              }
              setDialog(null);
            }}
            onClose={() => {
              setDialog(null);
            }}
          />
        )}
        {dialog?.kind === "drop" && (
          <DropPrompt
            catalog={catalog}
            object={dialog.ref}
            onSubmit={(layerId) => {
              editing?.addNode(layerId, dialog.ref);
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
                apply([dialog.fresh ? { op: "add_flow", flow } : { op: "set_flow", flow }]);
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
        {dialog?.kind === "load-kinds" && (
          <LoadKindsDialog
            catalog={catalog}
            editing={editing}
            onClose={() => {
              setDialog(null);
            }}
          />
        )}
        {dialog?.kind === "diagrams" && (
          <DiagramsDialog
            api={api}
            access={access}
            slice={{ node_ids: view?.node_ids ?? [], layer_ids: view?.layer_ids ?? [] }}
            onCreated={(created) => {
              setDialog(null);
              void navigate(`/views/${created.id}`);
            }}
            onClose={() => {
              setDialog(null);
            }}
          />
        )}
        {dialog?.kind === "drafts" && (
          <DraftsDialog
            access={access}
            drafts={drafts}
            onCreate={createDraft}
            onClose={() => {
              setDialog(null);
            }}
          />
        )}
      </div>
    </ReactFlowProvider>
  );
}

/** Вход без права читать процесс: только диаграммы, которыми поделились. */
function SharedOnly({ access, views }: { access: Access; views: View[] }): ReactElement {
  return (
    <div className="index" data-testid="shared-only" data-login={access.login}>
      <EmptyState title="no role to read the catalog">
        <p>Diagrams shared with you open here; ask an editor for a share or a catalog role.</p>
      </EmptyState>
      <section className="index__section" data-testid="shared-views">
        {views.length === 0 && <p className="index__empty">nothing is shared with you yet</p>}
        <ul className="index__list">
          {views.map((item) => (
            <li key={item.id} data-view={item.name}>
              <Link to={`/views/${item.id}`} className="index__link">
                {item.name}
              </Link>
              <Chip tone="muted">shared with you</Chip>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

type DropProps = {
  catalog: Catalog;
  object: ObjectRef;
  onSubmit: (layerId: string) => void;
  onClose: () => void;
};

/** Объект брошен мимо дорожек: слой выбирается здесь. */
function DropPrompt({ catalog, object, onSubmit, onClose }: DropProps): ReactElement {
  const [layerId, setLayerId] = useState(catalog.layers[0]?.id ?? "");

  return (
    <Dialog title="which layer?" mark="drop-layer" onClose={onClose}>
      <p className="form__note mono">{object.path.join("/")}</p>
      <Select
        aria-label="layer for the dropped object"
        value={layerId}
        onChange={(event) => {
          setLayerId(event.target.value);
        }}
      >
        {catalog.layers.map((layer) => (
          <option key={layer.id} value={layer.id}>
            {layer.name}
          </option>
        ))}
      </Select>
      <div className="form__actions">
        <Button
          tone="primary"
          disabled={layerId === ""}
          onClick={() => {
            onSubmit(layerId);
          }}
        >
          add node
        </Button>
        <Button tone="ghost" onClick={onClose}>
          cancel
        </Button>
      </div>
    </Dialog>
  );
}

/** Узкий экран: панели становятся ящиками поверх сцены, список по умолчанию закрыт. */
const NARROW_MAX_WIDTH = 900;

function narrowScreen(): boolean {
  return window.matchMedia(`(max-width: ${NARROW_MAX_WIDTH}px)`).matches;
}

type Base = Omit<Loaded, "catalog" | "title" | "version" | "draft" | "seq">;

function loadedOfDraft(state: DraftState, context: ProcessContext, base: Base): Loaded {
  return {
    ...base,
    catalog: new Catalog(state.snapshot, state.diff, context),
    title: state.draft.name,
    version: `draft · seq ${state.seq} · over v${state.draft.base_version}`,
    draft: state.draft,
    seq: state.seq,
  };
}

type LoadResult =
  | { kind: "denied"; access: Access; views: View[] }
  | { kind: "draft"; access: Access; state: DraftState; context: ProcessContext; currentVersion: number }
  | { kind: "view"; access: Access; state: ViewState; context: ProcessContext }
  | { kind: "published"; access: Access; snapshot: DraftState["snapshot"]; context: ProcessContext; version: number; drafts: Draft[] };

async function load(api: CatalogApi, source: PageSource): Promise<LoadResult> {
  const access = await api.access();
  if (source.kind === "draft") {
    const versions = await api.versions();
    const currentVersion = versions.at(-1)?.number ?? 0;
    const state = await api.draft(source.draftId);
    const context = await api.draftContext(source.draftId);
    return { kind: "draft", access, state, context, currentVersion };
  }

  if (source.kind === "view") {
    // вид приходит одним ответом со срезом каталога: прав на весь каталог не нужно
    const state = await api.viewState(source.viewId);
    const context = await api.viewContext(source.viewId);
    return { kind: "view", access, state, context };
  }

  if (!access.can_view) {
    const views = await api.views();
    return { kind: "denied", access, views };
  }

  const versions = await api.versions();
  const snapshot = await api.snapshot();
  const context = await api.context();
  const drafts = await api.drafts();
  return { kind: "published", access, snapshot, context, version: versions.at(-1)?.number ?? 0, drafts };
}

function loadedOfView(access: Access, state: ViewState, context: ProcessContext): Loaded {
  return {
    access,
    catalog: new Catalog(state.snapshot, undefined, context),
    title: state.view.name,
    version: `v${state.version}`,
    currentVersion: state.version,
    draft: undefined,
    drafts: [],
    view: state.view,
    saved: state.layout.positions,
    seq: 0,
    owned: state.owned,
  };
}

function loadedOfPublished(result: Extract<LoadResult, { kind: "published" }>): Loaded {
  return {
    access: result.access,
    catalog: new Catalog(result.snapshot, undefined, result.context),
    title: "process",
    version: `v${result.version}`,
    currentVersion: result.version,
    draft: undefined,
    drafts: result.drafts,
    view: undefined,
    saved: [],
    seq: 0,
    owned: false,
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
