import { Plus } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactElement } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, type CatalogApi } from "../api/client";
import { useServices } from "../app";
import type { Access, Source, SourceKind } from "../model/catalog";
import {
  Button,
  Chip,
  EmptyState,
  Eyebrow,
  Field,
  Form,
  Index,
  IndexHead,
  Input,
  List,
  ListAside,
  ListName,
  ListRow,
  Note,
  Select,
  TopbarLink,
  useToast,
} from "../ui";

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
    <Index mark="sources-page" data-can-edit={access.can_edit}>
      <IndexHead>
        <TopbarLink to="/">catalog</TopbarLink>
        <Eyebrow as="h4">sources</Eyebrow>
      </IndexHead>
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
      <List kind="spaced" mark="sources-list" empty="no sources yet">
        {sources.map((source) => (
          <ListRow key={source.id} data-source={source.name}>
            <ListName to={`/sources/${source.id}`}>{source.name}</ListName>
            <ListAside>
              <Chip tone="muted">{source.kind}</Chip>
              {source.manual && <Chip tone="draft">manual</Chip>}
              <Chip tone="muted">{source.latest_version === 0 ? "no versions" : `v${source.latest_version}`}</Chip>
              {source.description !== "" && (
                <Note micro tone="faint">
                  {source.description}
                </Note>
              )}
            </ListAside>
          </ListRow>
        ))}
      </List>
    </Index>
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
  const [kinds, setKinds] = useState<string[]>([]);
  const [kind, setKind] = useState<SourceKind>("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [params] = useSearchParams();
  const [manual, setManual] = useState(params.get("manual") === "1");
  const trimmed = name.trim();

  useEffect(() => {
    let cancelled = false;
    api
      .sourceKinds()
      .then((loaded) => {
        if (cancelled) {
          return;
        }

        setKinds(loaded);
        setKind((current) => (current === "" ? (loaded[0] ?? "") : current));
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          onError(error instanceof ApiError ? error.detail : String(error));
        }
      });

    return () => {
      cancelled = true;
    };
  }, [api, onError]);

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    if (trimmed === "" || kind === "") {
      return;
    }

    api
      .createSource({
        kind,
        name: trimmed,
        description: description.trim(),
        manual,
      })
      .then(onCreate)
      .catch((error: unknown) => {
        onError(error instanceof ApiError ? error.detail : String(error));
      });
  };

  return (
    <Form inline onSubmit={submit} mark="new-source">
      <Field label="kind">
        <Select
          fill
          aria-label="source kind"
          value={kind}
          onChange={(event) => {
            setKind(event.target.value);
          }}
        >
          {kinds.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="name" required>
        <Input
          mono
          fill
          aria-label="source name"
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </Field>
      <Field label="description">
        <Input
          fill
          aria-label="source description"
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
        />
      </Field>
      <Field label="manual" check>
        <input
          type="checkbox"
          aria-label="manual source"
          checked={manual}
          onChange={(event) => {
            setManual(event.target.checked);
          }}
        />
      </Field>
      <Button tone="primary" type="submit" icon={Plus} disabled={trimmed === "" || kind === ""}>
        source
      </Button>
    </Form>
  );
}
