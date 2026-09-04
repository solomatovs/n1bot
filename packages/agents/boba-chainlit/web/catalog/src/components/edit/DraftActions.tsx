import { useState, type ReactElement } from "react";

import { ApiError, type CatalogApi } from "../../api/client";
import type { Draft, RebaseIssue } from "../../model/catalog";
import { Alert, Button, Dialog, List, Note, Toolbar, useToast } from "../../ui";

type Props = {
  api: CatalogApi;
  draft: Draft;
  /** Текущая опубликованная версия; черновик над меньшей — устарел. */
  currentVersion: number;
  /** Сколько узлов и потоков разошлось с последними версиями источников. */
  staleCount: number;
  onChanged: () => void;
  /** Черновик отменён: страница уходит с него. */
  onDiscarded: () => void;
};

type Conflict = { current: number; issues: RebaseIssue[] | null };

/** Публикация, отмена и перебазирование черновика с двумя путями при конфликте:
 * обновить черновик из новой версии или вычеркнуть конфликтные операции. */
export function DraftActions({ api, draft, currentVersion, staleCount, onChanged, onDiscarded }: Props): ReactElement {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [discarding, setDiscarding] = useState(false);
  const stale = draft.base_version < currentVersion;

  const run = async (work: () => Promise<void>): Promise<void> => {
    setBusy(true);
    try {
      await work();
    } catch (error: unknown) {
      toast(error instanceof Error ? error.message : String(error), "error");
    } finally {
      setBusy(false);
    }
  };

  const publish = (): void => {
    void run(async () => {
      try {
        const version = await api.publish(draft.id);
        toast(`published as v${version.number}`, "success");
        onChanged();
      } catch (error: unknown) {
        if (error instanceof ApiError && error.status === 409) {
          setConflict({
            current: currentVersionOf(error) ?? currentVersion,
            issues: null,
          });
          return;
        }

        throw error;
      }
    });
  };

  const rebase = (drop: boolean): void => {
    void run(async () => {
      const result = await api.rebase(draft.id, drop);
      if (result.issues.length > 0 && !drop) {
        setConflict({ current: currentVersion, issues: result.issues });
        return;
      }

      setConflict(null);
      toast(drop ? `draft updated, ${result.issues.length} operation(s) dropped` : "draft updated", "success");
      onChanged();
    });
  };

  const bumpPins = (): void => {
    void run(async () => {
      const bump = await api.bumpPins(draft.id);
      if (bump.violations.length > 0) {
        toast(
          `pins raised; ${bump.violations.length} operation(s) no longer hold: ${bump.violations.join("; ")}`,
          "error",
        );
      } else {
        toast("pins raised to the latest source versions", "success");
      }
      onChanged();
    });
  };

  const discard = (): void => {
    void run(async () => {
      await api.discardDraft(draft.id);
      toast("draft discarded", "success");
      setDiscarding(false);
      onDiscarded();
    });
  };

  return (
    <>
      {stale && (
        <Button
          size="sm"
          tone="signal"
          disabled={busy}
          onClick={() => {
            rebase(false);
          }}
          data-testid="rebase-button"
        >
          update to v{currentVersion}
        </Button>
      )}
      {staleCount > 0 && (
        <Button size="sm" tone="signal" disabled={busy} onClick={bumpPins} data-testid="bump-pins-button">
          raise pins · {staleCount} stale
        </Button>
      )}
      <Button size="sm" tone="primary" disabled={busy} onClick={publish} data-testid="publish-button">
        publish
      </Button>
      <Button
        size="sm"
        tone="ghost"
        disabled={busy}
        onClick={() => {
          setDiscarding(true);
        }}
        data-testid="discard-button"
      >
        discard
      </Button>
      {discarding && (
        <Dialog
          title="discard the draft"
          mark="draft-discard"
          onClose={() => {
            setDiscarding(false);
          }}
        >
          <Alert tone="info">
            The draft “{draft.name}” with all its operations will be closed as discarded. The published catalog is not
            touched.
          </Alert>
          <Toolbar>
            <Button tone="danger" disabled={busy} onClick={discard}>
              discard the draft
            </Button>
            <Button
              tone="ghost"
              onClick={() => {
                setDiscarding(false);
              }}
            >
              keep editing
            </Button>
          </Toolbar>
        </Dialog>
      )}
      {conflict !== null && (
        <Dialog
          title="the catalog has moved on"
          mark="publish-conflict"
          onClose={() => {
            setConflict(null);
          }}
        >
          <Alert tone="info">
            The draft is based on v{draft.base_version}, the published catalog is at v{conflict.current}. Update the
            draft from the new version before publishing.
          </Alert>
          {conflict.issues !== null && (
            <div data-testid="rebase-issues">
              <Alert tone="error" title="these operations no longer apply">
                <List>
                  {conflict.issues.map((issue) => (
                    <li key={`${issue.seq}-${issue.index}`}>
                      <Note micro mono>
                        portion {issue.seq} · operation #{issue.index}: {issue.reason}
                      </Note>
                    </li>
                  ))}
                </List>
              </Alert>
            </div>
          )}
          <Toolbar>
            {conflict.issues === null ? (
              <Button
                tone="primary"
                disabled={busy}
                onClick={() => {
                  rebase(false);
                }}
              >
                update the draft
              </Button>
            ) : (
              <Button
                tone="danger"
                disabled={busy}
                onClick={() => {
                  rebase(true);
                }}
              >
                drop the conflicts and update
              </Button>
            )}
            <Button
              tone="ghost"
              onClick={() => {
                setConflict(null);
              }}
            >
              keep the draft as is
            </Button>
          </Toolbar>
        </Dialog>
      )}
    </>
  );
}

function currentVersionOf(error: ApiError): number | null {
  const payload = error.payload;
  if (typeof payload !== "object" || payload === null || !("detail" in payload)) {
    return null;
  }

  const detail: unknown = payload.detail;
  if (typeof detail !== "object" || detail === null || !("current_version" in detail)) {
    return null;
  }

  const current: unknown = detail.current_version;
  return typeof current === "number" ? current : null;
}
