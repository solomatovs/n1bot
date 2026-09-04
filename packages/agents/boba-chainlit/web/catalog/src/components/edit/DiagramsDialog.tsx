import { Plus } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactElement } from "react";

import { ApiError, type CatalogApi } from "../../api/client";
import type { Access, View } from "../../model/catalog";
import { Alert, Button, Chip, Dialog, Form, Input, List, ListAside, ListName, ListRow, useToast } from "../../ui";

type Props = {
  api: CatalogApi;
  access: Access;
  /** Срез, который сохранится новой диаграммой: узлы и слои текущего показа. */
  slice: { node_ids: string[]; layer_ids: string[] };
  onCreated: (view: View) => void;
  onClose: () => void;
};

type Loaded = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; views: View[] };

/** Диаграммы — сохранённые срезы процесса: список своих и расшаренных, ссылка
 * на каждую, форма новой диаграммы из текущего среза. */
export function DiagramsDialog({ api, access, slice, onCreated, onClose }: Props): ReactElement {
  const toast = useToast();
  const [state, setState] = useState<Loaded>({ status: "loading" });
  const [name, setName] = useState("");

  useEffect(() => {
    let cancelled = false;
    api
      .views()
      .then((views) => {
        if (!cancelled) {
          setState({ status: "ready", views });
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

  const create = (event: FormEvent): void => {
    event.preventDefault();
    const trimmed = name.trim();
    if (trimmed === "") {
      return;
    }

    api
      .createView({
        name: trimmed,
        node_ids: slice.node_ids,
        layer_ids: slice.layer_ids,
      })
      .then(onCreated)
      .catch((error: unknown) => {
        toast(error instanceof ApiError ? error.detail : String(error), "error");
      });
  };

  return (
    <Dialog title="diagrams" mark="diagrams" onClose={onClose}>
      {state.status === "failed" && <Alert tone="error">{state.message}</Alert>}
      {state.status === "ready" && (
        <List kind="spaced" mark="diagrams-list" empty="no diagrams yet">
          {state.views.map((view) => (
            <ListRow key={view.id} data-view={view.name}>
              <ListName to={`/views/${view.id}`}>{view.name}</ListName>
              <ListAside>
                <Chip tone="muted">{view.owner_id === access.user_id ? "yours" : "shared with you"}</Chip>
              </ListAside>
            </ListRow>
          ))}
        </List>
      )}
      {access.can_edit && (
        <Form inline onSubmit={create} mark="new-view">
          <Input
            mono
            fill
            placeholder="new diagram name"
            aria-label="new diagram name"
            value={name}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
          <Button tone="primary" type="submit" icon={Plus} disabled={name.trim() === ""}>
            save slice
          </Button>
        </Form>
      )}
    </Dialog>
  );
}
