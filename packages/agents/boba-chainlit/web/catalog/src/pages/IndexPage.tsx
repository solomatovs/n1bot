import { Plus } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactElement } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import type { Access, Draft, View } from "../model/catalog";
import { Button, Chip, EmptyState, Eyebrow, Input, useToast } from "../ui";

type Lists = { access: Access; views: View[]; drafts: Draft[] };
type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; lists: Lists };

/** Вход на страницу без адреса вида: доступные виды и открытые черновики.
 * Что показывать, решают права: без прав на каталог видны только расшаренные
 * виды, формы создания — только тому, кто вправе править. */
export function IndexPage(): ReactElement {
  const { api } = useServices();
  const toast = useToast();
  const navigate = useNavigate();
  const [state, setState] = useState<LoadState>({ status: "loading" });

  const fail = (error: unknown): void => {
    toast(error instanceof ApiError ? error.detail : String(error), "error");
  };

  const createDraft = (name: string): void => {
    api
      .createDraft(name)
      .then((draft) => {
        void navigate(`/drafts/${draft.id}`);
      })
      .catch(fail);
  };

  const createView = (name: string): void => {
    api
      .createView({ name, dataset_ids: [], layer_ids: [] })
      .then((view) => {
        void navigate(`/views/${view.id}`);
      })
      .catch(fail);
  };

  useEffect(() => {
    let cancelled = false;
    load(api)
      .then((lists) => {
        if (!cancelled) {
          setState({ status: "ready", lists });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const message = error instanceof ApiError ? error.detail : String(error);
          setState({ status: "failed", message });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api]);

  if (state.status === "loading") {
    return <EmptyState fill title="loading" />;
  }

  if (state.status === "failed") {
    return (
      <EmptyState fill title="the catalog is not available">
        {state.message}
      </EmptyState>
    );
  }

  const { access, views, drafts } = state.lists;

  return (
    <div className="index" data-testid="index-page" data-can-edit={access.can_edit}>
      <section className="index__section" data-testid="index-sources">
        <Eyebrow as="h4">sources</Eyebrow>
        <Link to="/sources" className="index__link">
          metadata sources
        </Link>
      </section>
      <section className="index__section" data-testid="index-views">
        <Eyebrow as="h4">views</Eyebrow>
        {access.can_edit && <NewNameForm mark="new-view" placeholder="new view name" label="view" onSubmit={createView} />}
        {views.length === 0 && <p className="index__empty">no views yet</p>}
        <ul className="index__list">
          {views.map((view) => (
            <li key={view.id} data-view={view.name}>
              <Link to={`/views/${view.id}`} className="index__link">
                {view.name}
              </Link>
              <Chip tone="muted">{view.owner_id === access.user_id ? "yours" : "shared with you"}</Chip>
            </li>
          ))}
        </ul>
      </section>
      {access.can_view && (
        <section className="index__section" data-testid="index-drafts">
          <Eyebrow as="h4">open drafts</Eyebrow>
          {access.can_edit && (
            <NewNameForm mark="new-draft" placeholder="new draft name" label="draft" onSubmit={createDraft} />
          )}
          {drafts.length === 0 && <p className="index__empty">no open drafts</p>}
          <ul className="index__list">
            {drafts.map((draft) => (
              <li key={draft.id}>
                <Link to={`/drafts/${draft.id}`} className="index__link">
                  {draft.name}
                </Link>
                <Chip tone="muted">over v{draft.base_version}</Chip>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

async function load(api: CatalogApi): Promise<Lists> {
  const access = await api.access();
  const views = await api.views();
  let drafts: Draft[] = [];
  if (access.can_view) {
    drafts = await api.drafts();
  }

  return { access, views, drafts };
}

type NewNameProps = {
  mark: string;
  placeholder: string;
  label: string;
  onSubmit: (name: string) => void;
};

/** Однострочная форма «имя + кнопка» для нового вида или черновика. */
function NewNameForm({ mark, placeholder, label, onSubmit }: NewNameProps): ReactElement {
  const [name, setName] = useState("");
  const trimmed = name.trim();

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    if (trimmed === "") {
      return;
    }

    onSubmit(trimmed);
  };

  return (
    <form className="index__new" onSubmit={submit} data-testid={mark}>
      <Input
        mono
        placeholder={placeholder}
        aria-label={placeholder}
        value={name}
        onChange={(event) => {
          setName(event.target.value);
        }}
      />
      <Button size="tiny" tone="primary" type="submit" icon={Plus} disabled={trimmed === ""}>
        {label}
      </Button>
    </form>
  );
}
