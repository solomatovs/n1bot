import { Pencil, Share2, Trash2 } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactElement } from "react";

import { ApiError, type CatalogApi } from "../../api/client";
import { Catalog, type Share, type Snapshot, type View, type ViewSpec } from "../../model/catalog";
import { Alert, Button, Chip, Field, IconButton, Input, Select, useToast } from "../../ui";
import { Dialog } from "./Dialog";

type Props = {
  api: CatalogApi;
  view: View;
  onChanged: () => void;
  onDeleted: () => void;
};

type Open = "none" | "edit" | "share" | "delete";

/** Кнопки владельца вида в шапке: фильтр и имя, шаринг, удаление.
 * Каждое действие — свой диалог; после успеха страница перечитывает вид. */
export function ViewActions({ api, view, onChanged, onDeleted }: Props): ReactElement {
  const toast = useToast();
  const [open, setOpen] = useState<Open>("none");
  const [busy, setBusy] = useState(false);

  const run = (work: () => Promise<void>): void => {
    setBusy(true);
    work()
      .then(() => {
        setOpen("none");
        onChanged();
      })
      .catch((error: unknown) => {
        toast(describe(error), "error");
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <>
      <IconButton
        aria-label="edit view"
        onClick={() => {
          setOpen("edit");
        }}
      >
        <Pencil size={14} />
      </IconButton>
      <IconButton
        aria-label="share view"
        onClick={() => {
          setOpen("share");
        }}
      >
        <Share2 size={14} />
      </IconButton>
      <IconButton
        aria-label="delete view"
        onClick={() => {
          setOpen("delete");
        }}
      >
        <Trash2 size={14} />
      </IconButton>
      {open === "edit" && (
        <ViewForm
          api={api}
          view={view}
          busy={busy}
          onSave={(spec) => {
            run(() => api.updateView(view.id, spec).then(() => undefined));
          }}
          onClose={() => {
            setOpen("none");
          }}
        />
      )}
      {open === "share" && (
        <SharesDialog
          api={api}
          view={view}
          onClose={() => {
            setOpen("none");
          }}
        />
      )}
      {open === "delete" && (
        <Dialog
          title="delete the view"
          mark="view-delete"
          onClose={() => {
            setOpen("none");
          }}
        >
          <Alert tone="info">
            The view “{view.name}” and its saved layout will be deleted. The catalog itself is not touched.
          </Alert>
          <div className="form__actions">
            <Button
              tone="danger"
              disabled={busy}
              onClick={() => {
                setBusy(true);
                api
                  .deleteView(view.id)
                  .then(() => {
                    toast("view deleted", "success");
                    onDeleted();
                  })
                  .catch((error: unknown) => {
                    toast(describe(error), "error");
                    setBusy(false);
                  });
              }}
            >
              delete the view
            </Button>
            <Button
              tone="ghost"
              onClick={() => {
                setOpen("none");
              }}
            >
              cancel
            </Button>
          </div>
        </Dialog>
      )}
    </>
  );
}

type FormProps = {
  api: CatalogApi;
  view: View;
  busy: boolean;
  onSave: (spec: ViewSpec) => void;
  onClose: () => void;
};

type Choices = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; catalog: Catalog };

/** Имя и фильтр вида: слои и узлы галочками по полному каталогу, который
 * владелец вправе читать. Пустой фильтр — весь каталог. */
function ViewForm({ api, view, busy, onSave, onClose }: FormProps): ReactElement {
  const [name, setName] = useState(view.name);
  const [layers, setLayers] = useState<Set<string>>(new Set(view.layer_ids));
  const [nodes, setNodes] = useState<Set<string>>(new Set(view.node_ids));
  const [choices, setChoices] = useState<Choices>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    api
      .snapshot()
      .then((snapshot: Snapshot) => {
        if (!cancelled) {
          setChoices({ status: "ready", catalog: new Catalog(snapshot) });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setChoices({ status: "failed", message: describe(error) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api]);

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    onSave({ name: name.trim(), layer_ids: [...layers], node_ids: [...nodes] });
  };

  return (
    <Dialog title="view" mark="view-form" onClose={onClose}>
      <form className="form" onSubmit={submit} data-testid="view-form">
        <Field label="name" required>
          <Input
            mono
            autoFocus
            aria-label="view name"
            value={name}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </Field>
        {choices.status === "loading" && <p className="form__note">loading the catalog…</p>}
        {choices.status === "failed" && <Alert tone="error">{choices.message}</Alert>}
        {choices.status === "ready" && (
          <>
            <ChoiceList
              mark="view-layers"
              label="layers"
              hint="whole layers; nothing checked means no layer filter"
              items={choices.catalog.layers.map((layer) => ({ id: layer.id, label: layer.name }))}
              chosen={layers}
              onChange={setLayers}
            />
            <ChoiceList
              mark="view-nodes"
              label="nodes"
              hint="single nodes on top of the layers"
              items={choices.catalog.nodes.map((node) => ({
                id: node.id,
                label: `${choices.catalog.layer(node.layer_id)?.name ?? "?"} / ${choices.catalog.label(node.id)}`,
              }))}
              chosen={nodes}
              onChange={setNodes}
            />
          </>
        )}
        <div className="form__actions">
          <Button tone="primary" type="submit" disabled={busy || name.trim() === ""}>
            save view
          </Button>
          <Button tone="ghost" onClick={onClose}>
            cancel
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

type ChoiceProps = {
  mark: string;
  label: string;
  hint: string;
  items: { id: string; label: string }[];
  chosen: Set<string>;
  onChange: (next: Set<string>) => void;
};

/** Список галочек под подписью поля; Field в режиме group, потому что у
 * каждой галочки label свой. */
function ChoiceList({ mark, label, hint, items, chosen, onChange }: ChoiceProps): ReactElement {
  const toggle = (id: string): void => {
    const next = new Set(chosen);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    onChange(next);
  };

  return (
    <Field label={label} hint={hint} group>
      <ul className="choices" data-testid={mark}>
        {items.map((item) => (
          <li key={item.id}>
            <label className="choices__item mono">
              <input
                type="checkbox"
                checked={chosen.has(item.id)}
                onChange={() => {
                  toggle(item.id);
                }}
              />
              {item.label}
            </label>
          </li>
        ))}
        {items.length === 0 && <li className="choices__empty">nothing to choose from</li>}
      </ul>
    </Field>
  );
}

type SharesProps = {
  api: CatalogApi;
  view: View;
  onClose: () => void;
};

type SharesState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; shares: Share[] };

/** Кому открыт вид: список выдач, добавление роли по имени или пользователя
 * по id, снятие выдачи. Шаринг даёт только просмотр. */
function SharesDialog({ api, view, onClose }: SharesProps): ReactElement {
  const toast = useToast();
  const [state, setState] = useState<SharesState>({ status: "loading" });
  const [kind, setKind] = useState<Share["kind"]>("role");
  const [target, setTarget] = useState("");

  const refresh = (): void => {
    api
      .shares(view.id)
      .then((shares) => {
        setState({ status: "ready", shares });
      })
      .catch((error: unknown) => {
        setState({ status: "failed", message: describe(error) });
      });
  };

  useEffect(refresh, [api, view.id]);

  const add = (event: FormEvent): void => {
    event.preventDefault();
    const trimmed = target.trim();
    if (trimmed === "") {
      return;
    }

    api
      .share(view.id, { kind, target: trimmed, mode: "view" })
      .then(() => {
        setTarget("");
        refresh();
      })
      .catch((error: unknown) => {
        toast(describe(error), "error");
      });
  };

  const remove = (share: Share): void => {
    api
      .unshare(view.id, share)
      .then(refresh)
      .catch((error: unknown) => {
        toast(describe(error), "error");
      });
  };

  return (
    <Dialog title="who can see this view" mark="view-shares" onClose={onClose}>
      {state.status === "failed" && <Alert tone="error">{state.message}</Alert>}
      {state.status === "ready" && (
        <ul className="shares" data-testid="shares-list">
          {state.shares.length === 0 && <li className="choices__empty">only you so far</li>}
          {state.shares.map((share) => (
            <li key={`${share.kind}:${share.target}`} className="shares__item" data-share={`${share.kind}:${share.target}`}>
              <Chip tone="muted">{share.kind}</Chip>
              <span className="mono">{share.target}</span>
              <IconButton
                aria-label={`revoke ${share.kind} ${share.target}`}
                onClick={() => {
                  remove(share);
                }}
              >
                <Trash2 size={12} />
              </IconButton>
            </li>
          ))}
        </ul>
      )}
      <form className="form shares__add" onSubmit={add} data-testid="share-form">
        <Select
          aria-label="share target kind"
          value={kind}
          onChange={(event) => {
            setKind(event.target.value === "user" ? "user" : "role");
          }}
        >
          <option value="role">role</option>
          <option value="user">user id</option>
        </Select>
        <Input
          mono
          aria-label="share target"
          placeholder={kind === "role" ? "role name" : "user id"}
          value={target}
          onChange={(event) => {
            setTarget(event.target.value);
          }}
        />
        <Button tone="primary" size="tiny" type="submit" disabled={target.trim() === ""}>
          share
        </Button>
      </form>
    </Dialog>
  );
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
