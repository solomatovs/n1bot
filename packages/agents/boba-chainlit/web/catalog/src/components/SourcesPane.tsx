import { ChevronDown, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useState, type ReactElement } from "react";
import { Link } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import type { ObjectRef, Source, TreeNode } from "../model/catalog";
import { Chip, IconButton } from "../ui";
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
          setState({ status: "failed", message: error instanceof ApiError ? error.detail : String(error) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api]);

  if (state.status === "loading") {
    return <p className="pane__empty">loading sources…</p>;
  }

  if (state.status === "failed") {
    return <p className="pane__empty">{state.message}</p>;
  }

  return (
    <div className="pane__scroll" data-testid="sources-pane">
      {state.sources.length === 0 && (
        <p className="pane__empty">
          no sources yet · <Link to="/sources">add one</Link>
        </p>
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
      <div className="pane__footer">
        <Link to="/sources" className="index__link" data-testid="sources-link">
          manage sources
        </Link>
      </div>
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
    <section className="pane__group" data-testid="source-branch" data-source={source.name} data-open={open}>
      <div className="pane__group-head">
        <IconButton
          aria-label={`${open ? "collapse" : "expand"} source ${source.name}`}
          onClick={() => {
            setOpen((current) => !current);
          }}
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </IconButton>
        <span className="pane__source mono">{source.name}</span>
        <Chip tone="muted">{source.kind}</Chip>
        {source.manual && <Chip tone="draft">manual</Chip>}
        <Chip tone="muted">{source.latest_version === 0 ? "no versions" : pinned}</Chip>
      </div>
      {open && source.latest_version > 0 && (
        <SourceTree load={load} reloadKey={`${source.id}:${version}`} selected={own} onSelect={select} draggable={draggable} />
      )}
      {open && source.latest_version === 0 && <p className="pane__empty">no versions yet</p>}
    </section>
  );
}
