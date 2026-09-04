import { Plus } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactElement } from "react";
import { Link } from "react-router-dom";

import { ApiError, type CatalogApi } from "../../api/client";
import type { Access, View } from "../../model/catalog";
import { Alert, Button, Chip, Input, useToast } from "../../ui";
import { Dialog } from "./Dialog";

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
          setState({ status: "failed", message: error instanceof ApiError ? error.detail : String(error) });
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
      .createView({ name: trimmed, node_ids: slice.node_ids, layer_ids: slice.layer_ids })
      .then(onCreated)
      .catch((error: unknown) => {
        toast(error instanceof ApiError ? error.detail : String(error), "error");
      });
  };

  return (
    <Dialog title="diagrams" mark="diagrams" onClose={onClose}>
      {state.status === "failed" && <Alert tone="error">{state.message}</Alert>}
      {state.status === "ready" && (
        <ul className="index__list" data-testid="diagrams-list">
          {state.views.length === 0 && <li className="choices__empty">no diagrams yet</li>}
          {state.views.map((view) => (
            <li key={view.id} data-view={view.name}>
              <Link to={`/views/${view.id}`} className="index__link">
                {view.name}
              </Link>
              <Chip tone="muted">{view.owner_id === access.user_id ? "yours" : "shared with you"}</Chip>
            </li>
          ))}
        </ul>
      )}
      {access.can_edit && (
        <form className="index__new" onSubmit={create} data-testid="new-view">
          <Input
            mono
            placeholder="new diagram name"
            aria-label="new diagram name"
            value={name}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
          <Button size="tiny" tone="primary" type="submit" icon={Plus} disabled={name.trim() === ""}>
            save slice
          </Button>
        </form>
      )}
    </Dialog>
  );
}
