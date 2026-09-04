import { ReactFlowProvider } from "@xyflow/react";
import { PanelLeft } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import { Canvas } from "../components/canvas/Canvas";
import { CanvasToolbar } from "../components/CanvasToolbar";
import { DetailPanel } from "../components/DetailPanel";
import { Dialog } from "../components/edit/Dialog";
import { DraftActions } from "../components/edit/DraftActions";
import { LayoutSaver } from "../model/layoutSaver";
import { ViewActions } from "../components/edit/ViewActions";
import { FlowForm } from "../components/edit/FlowForm";
import { NamePrompt } from "../components/edit/NamePrompt";
import { LeftPane } from "../components/LeftPane";
import {
  Catalog,
  type Dataset,
  type Draft,
  type DraftState,
  type Flow,
  type Layer,
  type NodePosition,
  type View,
  type ViewState,
} from "../model/catalog";
import { DraftEditor } from "../model/editor";
import type { EditActions } from "../model/editing";
import { datasetsInView, type GraphOptions, type ShowMode } from "../model/graph";
import { blankDataset, newId, removeDatasetWithFlows, type CatalogOp } from "../model/ops";
import { readUrlState, writeUrlState, type UrlState } from "../model/urlState";
import { Alert, Button, Chip, EmptyState, IconButton, useToast } from "../ui";

/** Что показывает страница: опубликованный каталог сквозь вид либо черновик. */
export type PageSource = { kind: "view"; viewId: string } | { kind: "draft"; draftId: string };

type Loaded = {
  catalog: Catalog;
  title: string;
  version: string;
  currentVersion: number;
  draft: Draft | undefined;
  view: View | undefined;
  saved: NodePosition[];
  /** Номер последней порции черновика; у вида 0. Виден тестам как data-seq. */
  seq: number;
  /** Вид принадлежит пользователю с правом правок: фильтр, шаринг, раскладка. */
  owned: boolean;
};

type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; loaded: Loaded };

/** Диалоги правок: имя новой сущности, форма потока. */
type DialogState =
  | { kind: "layer"; layer: Layer | undefined }
  | { kind: "dataset"; layerId: string }
  | { kind: "flow"; flow: Flow; fresh: boolean; pickTarget: boolean };

/** Страница диаграммы: загрузка снимка и вида, состояние в адресе, три панели;
 * на черновике — правки операциями, публикация и живое обновление по событиям. */
export function CatalogPage({ source }: { source: PageSource }): ReactElement {
  const { api } = useServices();
  const toast = useToast();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [params, setParams] = useSearchParams();
  const [paneOpen, setPaneOpen] = useState(() => !narrowScreen());
  const [tidyCount, setTidyCount] = useState(0);
  const [dialog, setDialog] = useState<DialogState | null>(null);
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

  const takeDraft = useCallback(
    (draftState: DraftState, currentVersion: number) => {
      setState({ status: "ready", loaded: loadedOfDraft(draftState, currentVersion) });
    },
    [],
  );

  // одинаковый ответ (своё же событие по SSE после правки) не перекладывает граф
  const lastLoaded = useRef("");

  const reload = useCallback(() => {
    let cancelled = false;
    load(api, source)
      .then((loaded) => {
        if (cancelled) {
          return;
        }

        const key = JSON.stringify(loaded);
        if (key === lastLoaded.current) {
          return;
        }
        lastLoaded.current = key;

        if (loaded.kind === "draft") {
          editor.current = new DraftEditor(api, loaded.state.draft.id, loaded.state, (next) => {
            takeDraft(next, loaded.currentVersion);
          });
          takeDraft(loaded.state, loaded.currentVersion);
          return;
        }

        editor.current = null;
        setState({ status: "ready", loaded: loadedOfView(loaded.state) });
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

  // живое обновление: чужие порции в этот черновик, новая версия, правка вида
  useEffect(() => {
    return api.events((message) => {
      if (source.kind === "draft" && message.draft_id === source.draftId) {
        void editor.current?.refresh().catch((error: unknown) => {
          toast(describe(error), "error");
        });
        return;
      }

      // новая версия: черновик мог устареть, вид показывает уже другой каталог
      if (message.version !== null) {
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

  const { catalog, draft, view, saved, currentVersion, seq, owned } = state.loaded;
  const editable = draft?.status === "open";
  const options: GraphOptions = {
    showMode: url.showMode,
    showDiff: draft !== undefined && url.showDiff,
    datasetIds: new Set(view?.dataset_ids ?? []),
    layerIds: new Set(view?.layer_ids ?? []),
    hidden: url.hidden,
  };
  const datasets = datasetsInView(catalog, options);
  const active = url.active === undefined ? undefined : catalog.dataset(url.active);

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
        addDataset: (layerId) => {
          setDialog({ kind: "dataset", layerId });
        },
        removeDataset: (dataset: Dataset) => {
          const flows = catalog.flowsOf(dataset.id);
          apply(removeDatasetWithFlows(dataset.id, [...flows.incoming, ...flows.outgoing]));
          update({ active: undefined });
        },
        newFlow: (from: Dataset) => {
          setDialog({ kind: "flow", flow: blankFlow(from.id, ""), fresh: true, pickTarget: true });
        },
        editFlow: (flow: Flow) => {
          setDialog({ kind: "flow", flow, fresh: false, pickTarget: false });
        },
      }
    : undefined;

  return (
    <ReactFlowProvider>
      <div className="page" data-testid="catalog-page" data-source={source.kind} data-editable={editable}
        data-owned={owned}
        data-seq={seq}
        data-layout-saves={layoutSaves}
      >
        <header className="topbar">
          <IconButton
            aria-label={paneOpen ? "hide the dataset list" : "show the dataset list"}
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
          {draft?.status === "open" && (
            <DraftActions
              api={api}
              draft={draft}
              currentVersion={currentVersion}
              onChanged={reload}
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
          <span className="topbar__spacer" />
          <span className="topbar__hint mono">
            {datasets.length} datasets · {catalog.flows.length} flows
          </span>
        </header>
        {draft !== undefined && draft.status !== "open" && (
          <Alert tone="info" mark="draft-closed">
            This draft is {draft.status}; it is read-only now.
          </Alert>
        )}
        <div className="page__body" data-pane={paneOpen} data-detail={active !== undefined}>
          {paneOpen && (
            <aside className="page__pane">
              <LeftPane
                catalog={catalog}
                datasets={datasets}
                activeId={url.active}
                hidden={url.hidden}
                showDiff={options.showDiff}
                editing={editing}
                onActivate={(id) => {
                  update({ active: id });
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
                update({ active: id });
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
              onMoved={
                view !== undefined && owned
                  ? (positions) => {
                      saver.schedule(view.id, positions);
                    }
                  : undefined
              }
            />
          </main>
          {active !== undefined && (
            <aside className="page__detail">
              <DetailPanel
                key={active.id}
                catalog={catalog}
                dataset={active}
                showDiff={options.showDiff}
                editing={editing}
                onActivate={(id) => {
                  update({ active: id });
                }}
                onClose={() => {
                  update({ active: undefined });
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
                apply([{ op: "add_layer", layer: { id: newId(), name } }]);
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
        {dialog?.kind === "dataset" && (
          <NamePrompt
            title="new dataset"
            mark="dataset-name"
            label="dataset name"
            initial=""
            onSubmit={(name) => {
              const dataset = blankDataset(dialog.layerId, name);
              apply([{ op: "add_dataset", dataset }]);
              update({ active: dataset.id });
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
      </div>
    </ReactFlowProvider>
  );
}

/** Узкий экран: панели становятся ящиками поверх сцены, список по умолчанию закрыт. */
const NARROW_MAX_WIDTH = 900;

function narrowScreen(): boolean {
  return window.matchMedia(`(max-width: ${NARROW_MAX_WIDTH}px)`).matches;
}

function blankFlow(from: string, to: string): Flow {
  return { id: newId(), from_dataset_id: from, to_dataset_id: to, load: { kind_id: "", values: {} }, description: "" };
}

function loadedOfDraft(state: DraftState, currentVersion: number): Loaded {
  return {
    catalog: new Catalog(state.snapshot, state.diff),
    title: state.draft.name,
    version: `draft · seq ${state.seq} · over v${state.draft.base_version}`,
    currentVersion,
    draft: state.draft,
    view: undefined,
    saved: [],
    seq: state.seq,
    owned: false,
  };
}

type LoadResult = { kind: "draft"; state: DraftState; currentVersion: number } | { kind: "view"; state: ViewState };

async function load(api: CatalogApi, source: PageSource): Promise<LoadResult> {
  if (source.kind === "draft") {
    const versions = await api.versions();
    const currentVersion = versions.at(-1)?.number ?? 0;
    const state = await api.draft(source.draftId);
    return { kind: "draft", state, currentVersion };
  }

  // вид приходит одним ответом со срезом каталога: прав на весь каталог не нужно
  const state = await api.viewState(source.viewId);
  return { kind: "view", state };
}

function loadedOfView(state: ViewState): Loaded {
  return {
    catalog: new Catalog(state.snapshot),
    title: state.view.name,
    version: `v${state.version}`,
    currentVersion: state.version,
    draft: undefined,
    view: state.view,
    saved: state.layout.positions,
    seq: 0,
    owned: state.owned,
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
