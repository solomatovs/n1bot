import { Copy, Plus, XCircle } from "lucide-react";
import { useCallback, type ReactElement } from "react";

import { ApiError } from "../../../api/transport";
import type { CatalogApi } from "../../api/client";
import { useServices } from "../../../services";
import { PageUrls } from "../../../config";
import { useLoadable } from "../../../hooks/useLoadable";
import type { Process, Share } from "../../model/catalog";
import { Button, Dialog, IconButton, List, ListAside, ListName, ListRow, Note, Toolbar, useToast } from "../../../ui";

type Props = {
  api: CatalogApi;
  process: Process;
  onClose: () => void;
};

/** Ссылки на просмотр процесса: действующие с адресом и отзывом, новая
 * ссылка. Гость по ней видит опубликованную версию без входа. */
export function ShareDialog({ api, process, onClose }: Props): ReactElement {
  const { urls } = useServices();
  const toast = useToast();
  const [state, reload] = useLoadable(
    useCallback(() => api.shares(process.id), [api, process.id]),
    { keep: true },
  );

  const linkOf = (share: Share): string => {
    const route = urls.absolute(PageUrls.catalog.shared(share.token));
    return `${window.location.origin}${route}`;
  };

  const copy = (share: Share): void => {
    navigator.clipboard
      .writeText(linkOf(share))
      .then(() => {
        toast("link copied", "success");
      })
      .catch((error: unknown) => {
        toast(ApiError.describe(error), "error");
      });
  };

  return (
    <Dialog title={`share ${process.name}`} mark="share" onClose={onClose}>
      <Note>Anyone with a link sees the published process, read-only, without signing in.</Note>
      {state.kind === "loading" && <Note mark="share-empty">loading links…</Note>}
      {state.kind === "error" && (
        <Note tone="error" mark="share-empty">
          {state.message}
        </Note>
      )}
      {state.kind === "ready" && (
        <List kind="spaced" mark="share-list" empty="no links yet">
          {state.value.map((share) => (
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
                        toast(ApiError.describe(error), "error");
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
                toast(ApiError.describe(error), "error");
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

