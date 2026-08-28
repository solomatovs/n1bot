import { Save, Trash2 } from "lucide-react";
import { type FormEvent, type ReactElement, useCallback, useState } from "react";
import { z } from "zod";

import { ApiError } from "../../api/client";
import { useServices } from "../../app";
import { errorText } from "../Async";
import { type ConnectionKind, ConnectionKindSchema, type ConnectionView } from "../../model/account";

type Props = {
  row: ConnectionView | null;
  onSaved: (saved: ConnectionView) => void;
  onRemoved: () => void;
};

/** Поля формы: общий плоский вид для трёх видов соединений; секрет вводится заново. */
type Draft = {
  name: string;
  kind: ConnectionKind;
  host: string;
  port: string;
  database: string;
  secure: boolean;
  user: string;
  password: string;
  baseUrl: string;
  sslVerify: boolean;
};

const KINDS: { value: ConnectionKind; label: string }[] = [
  { value: "postgres", label: "PostgreSQL" },
  { value: "clickhouse", label: "ClickHouse" },
  { value: "web", label: "Web" },
];

const StoredFieldsSchema = z
  .object({
    host: z.string().nullish(),
    port: z.number().nullish(),
    dbname: z.string().nullish(),
    database: z.string().nullish(),
    interface: z.string().nullish(),
    base_url: z.string().nullish(),
    ssl_verify: z.boolean().nullish(),
    auth: z.object({ method: z.string(), user: z.string().nullish() }).partial().nullish(),
  })
  .passthrough();

function draftOf(row: ConnectionView | null): Draft {
  if (row === null) {
    return {
      name: "",
      kind: "postgres",
      host: "",
      port: "",
      database: "",
      secure: false,
      user: "",
      password: "",
      baseUrl: "",
      sslVerify: true,
    };
  }

  const stored = StoredFieldsSchema.parse(row.profile);
  return {
    name: row.name,
    kind: row.kind,
    host: stored.host ?? "",
    port: stored.port === null || stored.port === undefined ? "" : String(stored.port),
    database: stored.dbname ?? stored.database ?? "",
    secure: stored.interface === "https",
    user: stored.auth?.user ?? "",
    password: "",
    baseUrl: stored.base_url ?? "",
    sslVerify: stored.ssl_verify ?? true,
  };
}

function portOf(draft: Draft): number | undefined {
  if (draft.port.trim() === "") {
    return undefined;
  }

  return Number(draft.port);
}

/** Профиль для api по виду: только то, что заполнено; пустой пароль — без auth. */
function profileOf(draft: Draft): Record<string, unknown> {
  if (draft.kind === "web") {
    const auth = draft.user === "" ? { method: "none" } : { method: "basic", user: draft.user, password: draft.password };
    return { kind: "web", base_url: draft.baseUrl, ssl_verify: draft.sslVerify, auth };
  }

  if (draft.kind === "clickhouse") {
    const auth =
      draft.password === ""
        ? { method: "no_password", user: draft.user }
        : { method: "password", user: draft.user, password: draft.password };
    return {
      kind: "clickhouse",
      host: draft.host,
      port: portOf(draft),
      interface: draft.secure ? "https" : "http",
      database: draft.database === "" ? undefined : draft.database,
      auth,
    };
  }

  const auth =
    draft.password === ""
      ? { method: "trust", user: draft.user }
      : { method: "password", user: draft.user, password: draft.password };
  return {
    kind: "postgres",
    host: draft.host,
    port: portOf(draft),
    dbname: draft.database === "" ? undefined : draft.database,
    auth,
  };
}

export function ConnectionForm({ row, onSaved, onRemoved }: Props): ReactElement {
  const { api } = useServices();
  const [draft, setDraft] = useState<Draft>(() => draftOf(row));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const readonly = row !== null && !row.mine;

  const patch = useCallback((change: Partial<Draft>) => {
    setDraft((current) => ({ ...current, ...change }));
  }, []);

  const fail = useCallback((failure: unknown) => {
    setBusy(false);
    if (failure instanceof ApiError) {
      setNotice(failure.detail);
      return;
    }

    setNotice(errorText(failure));
  }, []);

  const submit = useCallback(
    (event: FormEvent) => {
      event.preventDefault();
      setBusy(true);
      setNotice("");
      const body = { name: draft.name, profile: profileOf(draft) };
      const request = row === null ? api.createConnection(body) : api.replaceConnection(row.id, body);
      void request.then((saved) => {
        setBusy(false);
        setNotice("saved");
        onSaved(saved);
      }, fail);
    },
    [api, draft, row, onSaved, fail],
  );

  const remove = useCallback(() => {
    if (row === null) {
      return;
    }

    setBusy(true);
    void api.removeConnection(row.id).then(() => {
      setBusy(false);
      onRemoved();
    }, fail);
  }, [api, row, onRemoved, fail]);

  const text = (label: string, key: keyof Draft, type = "text"): ReactElement => (
    <label className="field">
      <span className="field__label">{label}</span>
      <input
        className="input"
        type={type}
        aria-label={`connection ${label}`}
        value={String(draft[key])}
        disabled={readonly}
        onChange={(event) => {
          patch({ [key]: event.target.value });
        }}
      />
    </label>
  );

  const flag = (label: string, key: "secure" | "sslVerify"): ReactElement => (
    <label className="field field--row">
      <input
        type="checkbox"
        aria-label={`connection ${label}`}
        checked={draft[key]}
        disabled={readonly}
        onChange={(event) => {
          patch({ [key]: event.target.checked });
        }}
      />
      <span className="field__label">{label}</span>
    </label>
  );

  return (
    <form className="form connection-form" onSubmit={submit} aria-label="connection">
      {readonly && <span className="notice">Shared connection: read-only</span>}
      {text("name", "name")}
      <label className="field">
        <span className="field__label">kind</span>
        <select
          className="input"
          aria-label="connection kind"
          value={draft.kind}
          disabled={row !== null}
          onChange={(event) => {
            patch({ kind: ConnectionKindSchema.parse(event.target.value) });
          }}
        >
          {KINDS.map((kind) => (
            <option key={kind.value} value={kind.value}>
              {kind.label}
            </option>
          ))}
        </select>
      </label>
      {draft.kind === "web" && (
        <>
          {text("base url", "baseUrl")}
          {flag("verify TLS", "sslVerify")}
          {text("user", "user")}
          {text("password", "password", "password")}
        </>
      )}
      {draft.kind !== "web" && (
        <>
          {text("host", "host")}
          {text("port", "port")}
          {text("database", "database")}
          {draft.kind === "clickhouse" && flag("https", "secure")}
          {text("user", "user")}
          {text("password", "password", "password")}
          {row !== null && <span className="field__hint">Secrets are not shown; enter the password again to keep it.</span>}
        </>
      )}
      {notice !== "" && (
        <span className={`notice${notice === "saved" ? "" : " notice--error"}`} data-notice="connection">
          {notice}
        </span>
      )}
      {!readonly && (
        <div className="connection-form__actions">
          <button type="submit" className="btn btn--primary" disabled={busy}>
            <Save size={14} />
            Save
          </button>
          {row !== null && (
            <button type="button" className="btn" disabled={busy} onClick={remove}>
              <Trash2 size={14} />
              Delete
            </button>
          )}
        </div>
      )}
    </form>
  );
}
