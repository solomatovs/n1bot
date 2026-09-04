import { ReactFlowProvider } from "@xyflow/react";
import { PanelLeft } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError } from "../api/client";
import { useServices } from "../app";
import { Canvas } from "../components/canvas/Canvas";
import { CanvasToolbar } from "../components/CanvasToolbar";
import { DetailPanel } from "../components/DetailPanel";
import { LeftPane } from "../components/LeftPane";
import { Catalog, type Draft, type NodePosition, type View } from "../model/catalog";
import { datasetsInView, type GraphOptions, type ShowMode } from "../model/graph";
import { readUrlState, writeUrlState, type UrlState } from "../model/urlState";
import { Button, Chip, EmptyState, IconButton } from "../ui";

/** Что показывает страница: опубликованный каталог сквозь вид либо черновик. */
export type PageSource = { kind: "view"; viewId: string } | { kind: "draft"; draftId: string };

type Loaded = {
  catalog: Catalog;
  title: string;
  version: string;
  draft: Draft | undefined;
  view: View | undefined;
  saved: NodePosition[];
};

type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; loaded: Loaded };

/** Страница диаграммы: загрузка снимка и вида, состояние в адресе, три панели. */
export function CatalogPage({ source }: { source: PageSource }): ReactElement {
  const { api } = useServices();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [params, setParams] = useSearchParams();
  const [paneOpen, setPaneOpen] = useState(() => !narrowScreen());
  const [tidyCount, setTidyCount] = useState(0);
  const url = useMemo(() => readUrlState(params), [params]);

  const update = useCallback(
    (patch: Partial<UrlState>) => {
      setParams((current) => writeUrlState({ ...readUrlState(current), ...patch }, current), { replace: true });
    },
    [setParams],
  );

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    load(api, source)
      .then((loaded) => {
        if (!cancelled) {
          setState({ status: "ready", loaded });
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
  }, [api, source]);

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

  const { catalog, draft, view, saved } = state.loaded;
  const options: GraphOptions = {
    showMode: url.showMode,
    showDiff: draft !== undefined && url.showDiff,
    datasetIds: new Set(view?.dataset_ids ?? []),
    layerIds: new Set(view?.layer_ids ?? []),
    hidden: url.hidden,
  };
  const datasets = datasetsInView(catalog, options);
  const active = url.active === undefined ? undefined : catalog.dataset(url.active);

  return (
    <ReactFlowProvider>
      <div className="page" data-testid="catalog-page" data-source={source.kind}>
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
          <span className="topbar__spacer" />
          <span className="topbar__hint mono">
            {datasets.length} datasets · {catalog.flows.length} flows
          </span>
        </header>
        <div className="page__body" data-pane={paneOpen} data-detail={active !== undefined}>
          {paneOpen && (
            <aside className="page__pane">
              <LeftPane
                catalog={catalog}
                datasets={datasets}
                activeId={url.active}
                hidden={url.hidden}
                showDiff={options.showDiff}
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
            />
          </main>
          {active !== undefined && (
            <aside className="page__detail">
              <DetailPanel
                catalog={catalog}
                dataset={active}
                showDiff={options.showDiff}
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
      </div>
    </ReactFlowProvider>
  );
}

/** Узкий экран: панели становятся ящиками поверх сцены, список по умолчанию закрыт. */
const NARROW_MAX_WIDTH = 900;

function narrowScreen(): boolean {
  return window.matchMedia(`(max-width: ${NARROW_MAX_WIDTH}px)`).matches;
}

async function load(api: ReturnType<typeof useServices>["api"], source: PageSource): Promise<Loaded> {
  if (source.kind === "draft") {
    const state = await api.draft(source.draftId);
    return {
      catalog: new Catalog(state.snapshot, state.diff),
      title: state.draft.name,
      version: `draft · seq ${state.seq} · over v${state.draft.base_version}`,
      draft: state.draft,
      view: undefined,
      saved: [],
    };
  }

  const [view, snapshot, layout, versions] = await Promise.all([
    api.view(source.viewId),
    api.snapshot(),
    api.layout(source.viewId),
    api.versions(),
  ]);
  const last = versions.at(-1);
  return {
    catalog: new Catalog(snapshot),
    title: view.name,
    version: last === undefined ? "v0" : `v${last.number}`,
    draft: undefined,
    view,
    saved: layout.positions,
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
