import { ChevronDown, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useState, type ReactElement } from "react";
import { Link } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import type { ObjectRef, Source, TreeNode } from "../model/catalog";
import { Button, Chip, IconButton, Note, Toolbar } from "../ui";
import { PaneGroup } from "./LeftPane";
import { SourceTree } from "./sources/SourceTree";

type Props = {
  api: CatalogApi;
  /** Версии источников, к которым привязан процесс; без привязки — последняя. */
  pins: Record<string, number>;
  selected: ObjectRef | undefined;
  onSelect: (ref: ObjectRef) => void;
  /** На черновике объекты тащатся на холст. */
  draggable: boolean;
};

type Loaded = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; sources: Source[] };

/** Источники метаданных в левой панели: каждый раскрывается в дерево той
 * версии, к которой привязан процесс; объект выбирается в панель деталей и
 * на черновике тащится в слой. */
export function SourcesPane({ api, pins, selected, onSelect, draggable }: Props): ReactElement {
  const [state, setState] = useState<Loaded>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    api
      .sources()
      .then((sources) => {
        if (!cancelled) {
          setState({ status: "ready", sources });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({
            status: "failed",
            message: error instanceof ApiError ? error.detail : String(error),
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api]);

  if (state.status === "loading") {
    return (
      <Note pad mark="pane-empty">
        loading sources…
      </Note>
    );
  }

  if (state.status === "failed") {
    return (
      <Note pad tone="error" mark="pane-empty">
        {state.message}
      </Note>
    );
  }

  return (
    <div className="pane__scroll" data-testid="sources-pane">
      {state.sources.length === 0 && (
        <Note pad mark="pane-empty">
          no sources yet · <Link to="/sources">add one</Link>
        </Note>
      )}
      {state.sources.map((source) => (
        <SourceBranch
          key={source.id}
          api={api}
          source={source}
          version={pins[source.id] ?? -1}
          selected={selected}
          onSelect={onSelect}
          draggable={draggable}
        />
      ))}
      <Toolbar pad>
        <Link to="/sources" data-testid="sources-link">
          <Button size="sm" tone="ghost">
            manage sources
          </Button>
        </Link>
      </Toolbar>
    </div>
  );
}

type BranchProps = {
  api: CatalogApi;
  source: Source;
  version: number;
  selected: ObjectRef | undefined;
  onSelect: (ref: ObjectRef) => void;
  draggable: boolean;
};

function SourceBranch({ api, source, version, selected, onSelect, draggable }: BranchProps): ReactElement {
  const [open, setOpen] = useState(false);
  const load = useCallback((path: string[]) => api.sourceTree(source.id, version, path), [api, source.id, version]);
  const pinned = version < 0 ? `v${source.latest_version}` : `v${version}`;
  const own = selected?.source_id === source.id ? selected : undefined;

  const select = (node: TreeNode): void => {
    if (node.ref !== null) {
      onSelect(node.ref);
    }
  };

  return (
    <PaneGroup
      title={source.name}
      name
      mark="source-branch"
      data={{ "data-source": source.name, "data-open": open }}
      lead={
        <IconButton
          size="sm"
          ghost
          aria-label={`${open ? "collapse" : "expand"} source ${source.name}`}
          onClick={() => {
            setOpen((current) => !current);
          }}
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </IconButton>
      }
      actions={
        <>
          <Chip tone="muted">{source.kind}</Chip>
          <Chip tone="muted">{source.latest_version === 0 ? "no versions" : pinned}</Chip>
        </>
      }
    >
      {open && (
        <>
          {source.latest_version > 0 ? (
            <SourceTree
              load={load}
              reloadKey={`${source.id}:${version}`}
              selected={own}
              onSelect={select}
              draggable={draggable}
            />
          ) : (
            <Note pad mark="pane-empty">
              no versions yet
            </Note>
          )}
        </>
      )}
    </PaneGroup>
  );
}
