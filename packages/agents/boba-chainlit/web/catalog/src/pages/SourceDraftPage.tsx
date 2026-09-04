import { Pencil, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useServices } from "../app";
import { ObjectCardPanel } from "../components/sources/ObjectCardPanel";
import { ObjectForm } from "../components/sources/ObjectForm";
import { SourceTree } from "../components/sources/SourceTree";
import type {
  Access,
  ManualObject,
  ObjectCard,
  ObjectRef,
  Source,
  SourceDraftState,
  SourceOp,
  TreeNode,
} from "../model/catalog";
import { RefParam } from "../model/refParam";
import { SourceDraftEditor } from "../model/sourceEditor";
import {
  Alert,
  Button,
  Chip,
  Dialog,
  EmptyState,
  IconButton,
  Page,
  PageBody,
  PageNotices,
  Pane,
  Scene,
  Toolbar,
  Topbar,
  TopbarHint,
  TopbarLink,
  TopbarSpacer,
  TopbarTitle,
  useToast,
} from "../ui";
import { describe } from "./SourcePage";

type Loaded = { access: Access; source: Source; state: SourceDraftState };
type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; loaded: Loaded };
type Panel =
  | { status: "empty" }
  | { status: "loading" }
  | { status: "failed"; message: string }
  | { status: "card"; card: ObjectCard };
type DialogState = { kind: "none" } | { kind: "add" } | { kind: "edit"; initial: ManualObject } | { kind: "discard" };

/** Черновик ручного источника: дерево свёрнутого снимка с пометками, карточка
 * объекта, форма объекта коротким набором полей, публикация версией. */
export function SourceDraftPage(): ReactElement {
  const { sourceId, draftId } = useParams();
  if (sourceId === undefined || draftId === undefined) {
    return <EmptyState fill title="draft id is missing" />;
  }

  return <DraftView sourceId={sourceId} draftId={draftId} />;
}

function DraftView({ sourceId, draftId }: { sourceId: string; draftId: string }): ReactElement {
  const { api } = useServices();
  const toast = useToast();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [panel, setPanel] = useState<Panel>({ status: "empty" });
  const [dialog, setDialog] = useState<DialogState>({ kind: "none" });
  const editor = useRef<SourceDraftEditor | null>(null);
  const selected = useMemo(() => RefParam.parse(params.get("ref")), [params]);

  const takeState = useCallback((draftState: SourceDraftState) => {
    setState((current) => {
      if (current.status !== "ready") {
        return current;
      }

      return {
        status: "ready",
        loaded: { ...current.loaded, state: draftState },
      };
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.access(), api.source(sourceId), api.sourceDraft(draftId)])
      .then(([access, source, draftState]) => {
        if (cancelled) {
          return;
        }

        editor.current = new SourceDraftEditor(api, draftId, draftState, takeState);
        setState({
          status: "ready",
          loaded: { access, source, state: draftState },
        });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({ status: "failed", message: describe(error) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, sourceId, draftId, takeState]);

  useEffect(() => {
    return api.events((message) => {
      if (message.source_id === sourceId) {
        void editor.current?.refresh().catch((error: unknown) => {
          toast(describe(error), "error");
        });
      }
    });
  }, [api, sourceId, toast]);

  const seq = state.status === "ready" ? state.loaded.state.seq : -1;
  const loadTree = useCallback((path: string[]) => api.sourceDraftTree(draftId, path), [api, draftId]);

  useEffect(() => {
    if (selected === undefined) {
      setPanel({ status: "empty" });
      return;
    }

    let cancelled = false;
    setPanel({ status: "loading" });
    api
      .sourceDraftObject(draftId, selected.kind, selected.path)
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
  }, [api, draftId, selected, seq]);

  const setRef = (ref: ObjectRef | undefined): void => {
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (ref === undefined) {
          next.delete("ref");
        } else {
          next.set("ref", RefParam.render(ref));
        }
        return next;
      },
      { replace: true },
    );
  };

  const apply = (ops: SourceOp[], then?: () => void): void => {
    const current = editor.current;
    if (current === null) {
      return;
    }

    current
      .apply(ops)
      .then((outcome) => {
        if (outcome.kind === "rejected") {
          toast(outcome.reason, "error");
          return;
        }

        then?.();
      })
      .catch((error: unknown) => {
        toast(describe(error), "error");
      });
  };

  if (state.status === "loading") {
    return <EmptyState fill title="loading the draft" />;
  }

  if (state.status === "failed") {
    return (
      <EmptyState fill title="the draft is not available">
        {state.message}
      </EmptyState>
    );
  }

  const { access, source, state: draftState } = state.loaded;
  const open = draftState.draft.status === "open" && access.can_edit;
  const card = panel.status === "card" ? panel.card : undefined;
  const editable = card !== undefined && (card.card === "pg_relation" || card.card === "ch_table");

  const select = (node: TreeNode): void => {
    if (node.ref !== null) {
      setRef(node.ref);
    }
  };

  const objectOf = (): ManualObject | undefined => {
    if (card === undefined) {
      return undefined;
    }

    if (card.card === "pg_relation") {
      return {
        kind: card.relation.kind === "view" ? "view" : "table",
        path: card.ref.path,
        comment: card.relation.comment,
        columns: card.columns.map((column) => ({
          name: column.name,
          type: column.type,
          nullable: column.nullable,
          comment: column.comment,
        })),
      };
    }

    if (card.card === "ch_table") {
      return {
        kind: card.table.kind === "view" ? "view" : "table",
        path: card.ref.path,
        comment: card.table.comment,
        columns: card.columns.map((column) => ({
          name: column.name,
          type: column.type,
          nullable: true,
          comment: column.comment,
        })),
      };
    }

    return undefined;
  };

  const actions =
    card !== undefined && open && editable ? (
      <>
        <IconButton
          size="sm"
          ghost
          aria-label="edit object"
          onClick={() => {
            const initial = objectOf();
            if (initial !== undefined) {
              setDialog({ kind: "edit", initial });
            }
          }}
        >
          <Pencil size={14} />
        </IconButton>
        <IconButton
          size="sm"
          ghost
          aria-label="remove object"
          onClick={() => {
            apply([{ op: "remove_object", path: card.ref.path }], () => {
              setRef(undefined);
            });
          }}
        >
          <Trash2 size={14} />
        </IconButton>
      </>
    ) : undefined;

  return (
    <Page mark="source-draft-page" data-source={source.name} data-seq={draftState.seq} data-editable={open}>
      <Topbar>
        <TopbarLink to="/">catalog</TopbarLink>
        <TopbarLink to={`/sources/${source.id}`}>{source.name}</TopbarLink>
        <TopbarTitle>{draftState.draft.name}</TopbarTitle>
        <Chip tone="muted">{`draft · seq ${draftState.seq} · over v${draftState.draft.base_version}`}</Chip>
        {open && (
          <>
            <Button
              size="sm"
              icon={Plus}
              onClick={() => {
                setDialog({ kind: "add" });
              }}
              data-testid="add-object"
            >
              object
            </Button>
            <Button
              size="sm"
              tone="primary"
              data-testid="publish-source-draft"
              onClick={() => {
                api
                  .publishSourceDraft(draftId)
                  .then((version) => {
                    toast(`published as v${version.version}`, "success");
                    void navigate(`/sources/${source.id}?v=${version.version}`);
                  })
                  .catch((error: unknown) => {
                    toast(describe(error), "error");
                  });
              }}
            >
              publish
            </Button>
            <Button
              size="sm"
              tone="ghost"
              data-testid="discard-source-draft"
              onClick={() => {
                setDialog({ kind: "discard" });
              }}
            >
              discard
            </Button>
          </>
        )}
        <TopbarSpacer />
        <TopbarHint>{draftState.diff.entries.length} changes</TopbarHint>
      </Topbar>
      <PageNotices>
        {draftState.draft.status !== "open" && (
          <Alert tone="info" mark="source-draft-closed">
            This draft is {draftState.draft.status}; it is read-only now.
          </Alert>
        )}
      </PageNotices>
      <PageBody pane={true} detail={false}>
        <Pane>
          <SourceTree load={loadTree} reloadKey={String(draftState.seq)} selected={selected} onSelect={select} />
        </Pane>
        <Scene panel>
          {panel.status === "empty" && (
            <EmptyState fill title="pick an object in the tree">
              {open ? "or add a new one" : ""}
            </EmptyState>
          )}
          {panel.status === "loading" && <EmptyState fill title="loading the object" />}
          {panel.status === "failed" && (
            <EmptyState fill title="the object is not available">
              {panel.message}
            </EmptyState>
          )}
          {card !== undefined && <ObjectCardPanel card={card} actions={actions} />}
        </Scene>
      </PageBody>
      {dialog.kind === "add" && (
        <ObjectForm
          kind={source.kind}
          initial={undefined}
          onSave={(object) => {
            setDialog({ kind: "none" });
            apply([{ op: "add_object", object }], () => {
              setRef({
                source_id: source.id,
                kind: source.kind === "postgres" ? "relation" : "table",
                path: object.path,
              });
            });
          }}
          onClose={() => {
            setDialog({ kind: "none" });
          }}
        />
      )}
      {dialog.kind === "edit" && (
        <ObjectForm
          kind={source.kind}
          initial={dialog.initial}
          onSave={(object) => {
            setDialog({ kind: "none" });
            apply([{ op: "set_object", object }]);
          }}
          onClose={() => {
            setDialog({ kind: "none" });
          }}
        />
      )}
      {dialog.kind === "discard" && (
        <Dialog
          title="discard the draft"
          mark="source-draft-discard"
          onClose={() => {
            setDialog({ kind: "none" });
          }}
        >
          <Alert tone="info">The draft “{draftState.draft.name}” will be closed as discarded.</Alert>
          <Toolbar>
            <Button
              tone="danger"
              onClick={() => {
                api
                  .discardSourceDraft(draftId)
                  .then(() => {
                    toast("draft discarded", "success");
                    void navigate(`/sources/${source.id}`);
                  })
                  .catch((error: unknown) => {
                    toast(describe(error), "error");
                  });
              }}
            >
              discard the draft
            </Button>
            <Button
              tone="ghost"
              onClick={() => {
                setDialog({ kind: "none" });
              }}
            >
              keep editing
            </Button>
          </Toolbar>
        </Dialog>
      )}
    </Page>
  );
}
