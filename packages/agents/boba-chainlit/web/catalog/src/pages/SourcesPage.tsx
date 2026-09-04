import { Plus } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactElement } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import type { Access, Source, SourceKind } from "../model/catalog";
import { Button, Chip, EmptyState, Eyebrow, Field, Input, Select, useToast } from "../ui";

type Lists = { access: Access; sources: Source[] };
type LoadState = { status: "loading" } | { status: "failed"; message: string } | { status: "ready"; lists: Lists };

/** Источники метаданных: список с видом и последней версией, форма нового
 * источника для того, кто вправе править каталог. */
export function SourcesPage(): ReactElement {
  const { api } = useServices();
  const toast = useToast();
  const navigate = useNavigate();
  const [state, setState] = useState<LoadState>({ status: "loading" });

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
          setState({ status: "failed", message: error instanceof ApiError ? error.detail : String(error) });
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
      <EmptyState fill title="sources are not available">
        {state.message}
      </EmptyState>
    );
  }

  const { access, sources } = state.lists;

  return (
    <div className="index" data-testid="sources-page" data-can-edit={access.can_edit}>
      <header className="index__head">
        <Link to="/" className="topbar__home">
          catalog
        </Link>
        <Eyebrow as="h4">sources</Eyebrow>
      </header>
      {access.can_edit && (
        <NewSourceForm
          onCreate={(source) => {
            void navigate(`/sources/${source.id}`);
          }}
          onError={(message) => {
            toast(message, "error");
          }}
        />
      )}
      {sources.length === 0 && <p className="index__empty">no sources yet</p>}
      <ul className="index__list" data-testid="sources-list">
        {sources.map((source) => (
          <li key={source.id} data-source={source.name}>
            <Link to={`/sources/${source.id}`} className="index__link">
              {source.name}
            </Link>
            <Chip tone="muted">{source.kind}</Chip>
            {source.manual && <Chip tone="draft">manual</Chip>}
            <Chip tone="muted">{source.latest_version === 0 ? "no versions" : `v${source.latest_version}`}</Chip>
            {source.description !== "" && <span className="index__note">{source.description}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

async function load(api: CatalogApi): Promise<Lists> {
  const access = await api.access();
  const sources = await api.sources();
  return { access, sources };
}

type FormProps = {
  onCreate: (source: Source) => void;
  onError: (message: string) => void;
};

function NewSourceForm({ onCreate, onError }: FormProps): ReactElement {
  const { api } = useServices();
  const [kind, setKind] = useState<SourceKind>("postgres");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [params] = useSearchParams();
  const [manual, setManual] = useState(params.get("manual") === "1");
  const trimmed = name.trim();

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    if (trimmed === "") {
      return;
    }

    api
      .createSource({ kind, name: trimmed, description: description.trim(), manual })
      .then(onCreate)
      .catch((error: unknown) => {
        onError(error instanceof ApiError ? error.detail : String(error));
      });
  };

  return (
    <form className="form form--inline" onSubmit={submit} data-testid="new-source">
      <Field label="kind">
        <Select
          aria-label="source kind"
          value={kind}
          onChange={(event) => {
            setKind(event.target.value === "clickhouse" ? "clickhouse" : "postgres");
          }}
        >
          <option value="postgres">postgres</option>
          <option value="clickhouse">clickhouse</option>
        </Select>
      </Field>
      <Field label="name" required>
        <Input
          mono
          aria-label="source name"
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </Field>
      <Field label="description">
        <Input
          aria-label="source description"
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
        />
      </Field>
      <Field label="manual" controlFirst row>
        <input
          type="checkbox"
          aria-label="manual source"
          checked={manual}
          onChange={(event) => {
            setManual(event.target.checked);
          }}
        />
      </Field>
      <Button size="tiny" tone="primary" type="submit" icon={Plus} disabled={trimmed === ""}>
        source
      </Button>
    </form>
  );
}
