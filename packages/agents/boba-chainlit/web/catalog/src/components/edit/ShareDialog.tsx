import { Copy, Plus, XCircle } from "lucide-react";
import { useCallback, useEffect, useState, type ReactElement } from "react";

import { ApiError, type CatalogApi } from "../../api/client";
import { useServices } from "../../app";
import type { Process, Share } from "../../model/catalog";
import { Button, Dialog, IconButton, List, ListAside, ListName, ListRow, Note, Toolbar, useToast } from "../../ui";

type Props = {
  api: CatalogApi;
  process: Process;
  onClose: () => void;
};

type Loaded = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; shares: Share[] };

/** Ссылки на просмотр процесса: действующие с адресом и отзывом, новая
 * ссылка. Гость по ней видит опубликованную версию без входа. */
export function ShareDialog({ api, process, onClose }: Props): ReactElement {
  const { urls } = useServices();
  const toast = useToast();
  const [state, setState] = useState<Loaded>({ status: "loading" });

  const reload = useCallback(() => {
    api
      .shares(process.id)
      .then((shares) => {
        setState({ status: "ready", shares });
      })
      .catch((error: unknown) => {
        setState({ status: "failed", message: describe(error) });
      });
  }, [api, process.id]);

  useEffect(() => {
    reload();
  }, [reload]);

  const linkOf = (share: Share): string => `${window.location.origin}${urls.routerBase}/shared/${share.token}`;

  const copy = (share: Share): void => {
    navigator.clipboard
      .writeText(linkOf(share))
      .then(() => {
        toast("link copied", "success");
      })
      .catch((error: unknown) => {
        toast(describe(error), "error");
      });
  };

  return (
    <Dialog title={`share ${process.name}`} mark="share" onClose={onClose}>
      <Note>Anyone with a link sees the published process, read-only, without signing in.</Note>
      {state.status === "loading" && <Note mark="share-empty">loading links…</Note>}
      {state.status === "failed" && (
        <Note tone="error" mark="share-empty">
          {state.message}
        </Note>
      )}
      {state.status === "ready" && (
        <List kind="spaced" mark="share-list" empty="no links yet">
          {state.shares.map((share) => (
            <ListRow key={share.token} data-token={share.token}>
              <ListName title={linkOf(share)}>
                {linkOf(share)}
              </ListName>
              <ListAside>
                <IconButton
                  size="sm"
                  ghost
                  aria-label={`copy link ${share.token}`}
                  onClick={() => {
                    copy(share);
                  }}
                >
                  <Copy size={14} />
                </IconButton>
                <IconButton
                  size="sm"
                  ghost
                  aria-label={`revoke link ${share.token}`}
                  onClick={() => {
                    api
                      .revokeShare(share.token)
                      .then(() => {
                        toast("link revoked", "success");
                        reload();
                      })
                      .catch((error: unknown) => {
                        toast(describe(error), "error");
                      });
                  }}
                >
                  <XCircle size={14} />
                </IconButton>
              </ListAside>
            </ListRow>
          ))}
        </List>
      )}
      <Toolbar>
        <Button
          tone="primary"
          icon={Plus}
          onClick={() => {
            api
              .share(process.id)
              .then(() => {
                toast("link created", "success");
                reload();
              })
              .catch((error: unknown) => {
                toast(describe(error), "error");
              });
          }}
          data-testid="new-share"
        >
          new link
        </Button>
        <Button tone="ghost" onClick={onClose}>
          close
        </Button>
      </Toolbar>
    </Dialog>
  );
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail;
  }

  return String(error);
}
