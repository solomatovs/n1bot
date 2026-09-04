import { GitCompare, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import { Dialog } from "../components/edit/Dialog";
import { NamePrompt } from "../components/edit/NamePrompt";
import { DiffPanel } from "../components/sources/DiffPanel";
import { ObjectCardPanel } from "../components/sources/ObjectCardPanel";
import { SourceTree } from "../components/sources/SourceTree";
import type { Access, ObjectCard, ObjectRef, Source, SourceDiff, SourceDraft, SourceVersion, TreeNode } from "../model/catalog";
import { RefParam } from "../model/refParam";
import { Alert, Button, Chip, EmptyState, IconButton, Select, useToast } from "../ui";

type Loaded = { access: Access; source: Source; versions: SourceVersion[]; drafts: SourceDraft[] };
type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; loaded: Loaded };
type Panel = { status: "empty" } | { status: "loading" } | { status: "failed"; message: string } | { status: "card"; card: ObjectCard };

/** Страница источника: дерево версии слева, родная карточка объекта справа,
 * выбор версии и разница с предыдущей, у ручного источника — черновики. */
export function SourcePage(): ReactElement {
  const { sourceId } = useParams();
  if (sourceId === undefined) {
    return <EmptyState fill title="source id is missing" />;
  }

  return <SourceView sourceId={sourceId} />;
}

function SourceView({ sourceId }: { sourceId: string }): ReactElement {
  const { api } = useServices();
  const toast = useToast();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [panel, setPanel] = useState<Panel>({ status: "empty" });
  const [diff, setDiff] = useState<SourceDiff | null>(null);
  const [dialog, setDialog] = useState<"none" | "draft" | "delete">("none");
  const [reloads, setReloads] = useState(0);

  const requestedVersion = Number(params.get("v") ?? "-1");
  const showDiff = params.get("mode") === "diff";
  const selected = useMemo(() => RefParam.parse(params.get("ref")), [params]);

  const reload = useCallback(() => {
    let cancelled = false;
    load(api, sourceId)
      .then((loaded) => {
        if (!cancelled) {
          setState({ status: "ready", loaded });
          setReloads((count) => count + 1);
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
  }, [api, sourceId]);

  useEffect(() => reload(), [reload]);

  useEffect(() => {
    return api.events((message) => {
      if (message.source_id === sourceId) {
        reload();
      }
    });
  }, [api, sourceId, reload]);

  const version = state.status === "ready" ? resolveVersion(state.loaded.source, requestedVersion) : requestedVersion;

  const loadTree = useCallback((path: string[]) => api.sourceTree(sourceId, version, path), [api, sourceId, version]);

  useEffect(() => {
    if (selected === undefined) {
      setPanel({ status: "empty" });
      return;
    }

    let cancelled = false;
    setPanel({ status: "loading" });
    api
      .sourceObject(sourceId, version, selected.kind, selected.path)
      .then((card) => {
        if (!cancelled) {
          setPanel({ status: "card", card });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setPanel({ status: "failed", message: describe(error) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, sourceId, version, selected, reloads]);

  useEffect(() => {
    if (!showDiff || version < 2) {
      setDiff(null);
      return;
    }

    let cancelled = false;
    api
      .sourceDiff(sourceId, version - 1, version)
      .then((loaded) => {
        if (!cancelled) {
          setDiff(loaded);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          toast(describe(error), "error");
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, sourceId, version, showDiff, toast, reloads]);

  if (state.status === "loading") {
    return <EmptyState fill title="loading the source" />;
  }

  if (state.status === "failed") {
    return (
      <EmptyState fill title="the source is not available">
        {state.message}
      </EmptyState>
    );
  }

  const { access, source, versions, drafts } = state.loaded;
  const setParam = (patch: Record<string, string | undefined>): void => {
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        for (const [key, value] of Object.entries(patch)) {
          if (value === undefined) {
            next.delete(key);
          } else {
            next.set(key, value);
          }
        }
        return next;
      },
      { replace: true },
    );
  };

  const select = (node: TreeNode): void => {
    if (node.ref === null) {
      return;
    }

    setParam({ ref: RefParam.render(node.ref), mode: undefined });
  };

  return (
    <div className="page" data-testid="source-page" data-source={source.name} data-version={version} data-can-edit={access.can_edit}>
      <header className="topbar">
        <Link to="/" className="topbar__home">
          catalog
        </Link>
        <Link to="/sources" className="topbar__home">
          sources
        </Link>
        <span className="topbar__title" data-testid="page-title">
          {source.name}
        </span>
        <Chip tone="muted">{source.kind}</Chip>
        {source.manual && <Chip tone="draft">manual</Chip>}
        <Select
          aria-label="source version"
          value={String(version)}
          onChange={(event) => {
            setParam({ v: event.target.value, mode: undefined });
          }}
        >
          {versions.length === 0 && <option value="0">no versions</option>}
          {versions.map((item) => (
            <option key={item.version} value={String(item.version)}>
              v{item.version} · {item.objects_total} objects · {item.taken_at.slice(0, 16).replace("T", " ")}
            </option>
          ))}
        </Select>
        {version >= 2 && (
          <Button
            size="tiny"
            tone={showDiff ? "signal" : "ghost"}
            icon={GitCompare}
            aria-pressed={showDiff}
            onClick={() => {
              setParam({ mode: showDiff ? undefined : "diff" });
            }}
          >
            diff with v{version - 1}
          </Button>
        )}
        {source.manual && access.can_edit && (
          <Button
            size="tiny"
            tone="primary"
            icon={Plus}
            onClick={() => {
              setDialog("draft");
            }}
            data-testid="new-source-draft"
          >
            draft
          </Button>
        )}
        {access.can_edit && (
          <IconButton
            aria-label="delete source"
            onClick={() => {
              setDialog("delete");
            }}
          >
            <Trash2 size={14} />
          </IconButton>
        )}
        <span className="topbar__spacer" />
        <span className="topbar__hint mono">{source.description}</span>
      </header>
      {drafts.length > 0 && (
        <Alert tone="info" mark="source-drafts">
          open drafts:{" "}
          {drafts.map((draft) => (
            <Link key={draft.id} to={`/sources/${source.id}/drafts/${draft.id}`} className="index__link">
              {draft.name}
            </Link>
          ))}
        </Alert>
      )}
      <div className="page__body" data-pane={true} data-detail={panel.status !== "empty" || diff !== null}>
        <aside className="page__pane">
          {versions.length === 0 ? (
            <EmptyState title="no versions yet">
              {source.manual ? "create a draft and add objects" : "run a synchronisation"}
            </EmptyState>
          ) : (
            <SourceTree load={loadTree} reloadKey={`${version}:${reloads}`} selected={selected} onSelect={select} />
          )}
        </aside>
        <main className="page__scene page__scene--panel">
          {diff !== null && showDiff ? (
            <DiffPanel diff={diff} title={`v${version - 1} → v${version}`} />
          ) : (
            <ObjectPanel panel={panel} />
          )}
        </main>
      </div>
      {dialog === "draft" && (
        <NamePrompt
          title="new draft"
          mark="source-draft-name"
          label="draft name"
          initial=""
          onSubmit={(name) => {
            api
              .createSourceDraft(source.id, name)
              .then((draft) => {
                setDialog("none");
                void navigate(`/sources/${source.id}/drafts/${draft.id}`);
              })
              .catch((error: unknown) => {
                toast(describe(error), "error");
              });
          }}
          onClose={() => {
            setDialog("none");
          }}
        />
      )}
      {dialog === "delete" && (
        <Dialog
          title="delete the source"
          mark="source-delete"
          onClose={() => {
            setDialog("none");
          }}
        >
          <Alert tone="info">The source “{source.name}” with all its versions will be deleted.</Alert>
          <div className="form__actions">
            <Button
              tone="danger"
              onClick={() => {
                api
                  .deleteSource(source.id)
                  .then(() => {
                    toast("source deleted", "success");
                    void navigate("/sources");
                  })
                  .catch((error: unknown) => {
                    toast(describe(error), "error");
                  });
              }}
            >
              delete the source
            </Button>
            <Button
              tone="ghost"
              onClick={() => {
                setDialog("none");
              }}
            >
              cancel
            </Button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

export function ObjectPanel({ panel }: { panel: Panel }): ReactElement {
  if (panel.status === "empty") {
    return <EmptyState fill title="pick an object in the tree" />;
  }

  if (panel.status === "loading") {
    return <EmptyState fill title="loading the object" />;
  }

  if (panel.status === "failed") {
    return (
      <EmptyState fill title="the object is not available">
        {panel.message}
      </EmptyState>
    );
  }

  return <ObjectCardPanel card={panel.card} />;
}

function resolveVersion(source: Source, requested: number): number {
  if (requested < 0 || requested > source.latest_version) {
    return source.latest_version;
  }

  return requested;
}

async function load(api: CatalogApi, sourceId: string): Promise<Loaded> {
  const access = await api.access();
  const source = await api.source(sourceId);
  const versions = await api.sourceVersions(sourceId);
  let drafts: SourceDraft[] = [];
  if (source.manual) {
    drafts = await api.sourceDrafts(sourceId);
  }

  return { access, source, versions, drafts };
}

export function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return String(error);
}

export type { ObjectRef };
