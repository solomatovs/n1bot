import { Save, Trash2 } from "lucide-react";
import { type FormEvent, type ReactElement, useCallback, useState } from "react";

import { ApiError } from "../../api/client";
import { useServices } from "../../app";
import { Alert } from "../Alert";
import { errorText } from "../Async";
import type { ConnectionView } from "../../model/account";
import { type SchemaDoc, withoutMaskedSecrets } from "../../model/schema";
import { SchemaNode } from "./SchemaForm";

type Props = {
  doc: SchemaDoc;
  row: ConnectionView | null;
  onSaved: (saved: ConnectionView) => void;
  onRemoved: () => void;
};

/** Форма соединения по схеме api: имя и профиль с любой вложенностью; секреты заново. */
export function ConnectionForm({ doc, row, onSaved, onRemoved }: Props): ReactElement {
  const { api } = useServices();
  const [name, setName] = useState(row?.name ?? "");
  const [profile, setProfile] = useState<unknown>(() =>
    row === null ? doc.defaults(doc.root) : withoutMaskedSecrets(row.profile),
  );
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [issues, setIssues] = useState<ReadonlyMap<string, string>>(new Map());
  const readonly = row !== null && !row.mine;

  const fail = useCallback(
    (failure: unknown) => {
      setBusy(false);
      if (!(failure instanceof ApiError)) {
        setNotice(errorText(failure));
        return;
      }

      // ошибки полей — под полями, остальное (и ошибки без поля) — в общем сообщении
      const byField = new Map<string, string>();
      const general: string[] = [];
      for (const issue of failure.issues) {
        const path = issue.loc[0] === "profile" ? doc.formPath("profile", issue.loc.slice(1)) : issue.loc.join(".");
        if (path === "profile" || path === "") {
          general.push(issue.message);
          continue;
        }

        byField.set(path, issue.message);
      }

      setIssues(byField);
      if (general.length > 0) {
        setNotice(general.join("\n"));
        return;
      }

      setNotice(byField.size > 0 ? `Check ${byField.size} field(s) marked below` : failure.detail);
    },
    [doc],
  );

  const submit = useCallback(
    (event: FormEvent) => {
      event.preventDefault();
      setBusy(true);
      setNotice("");
      setIssues(new Map());
      const body = { name, profile: profile as Record<string, unknown> };
      const request = row === null ? api.createConnection(body) : api.replaceConnection(row.id, body);
      void request.then((saved) => {
        setBusy(false);
        onSaved(saved);
      }, fail);
    },
    [api, name, profile, row, onSaved, fail],
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

  return (
    <form className="form connection-form" onSubmit={submit} aria-label="connection">
      {readonly && <Alert tone="info">Shared connection: read-only</Alert>}
      <label className="field">
        <span className="field__label">
          name<span className="field__required">*</span>
        </span>
        <input
          className="input"
          aria-label="connection name"
          value={name}
          disabled={readonly}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </label>
      <SchemaNode
        doc={doc}
        schema={doc.root}
        value={profile}
        path="profile"
        label="profile"
        required
        readonly={readonly}
        issues={issues}
        onChange={setProfile}
      />
      {row !== null && !readonly && (
        <Alert tone="info">Secrets are not shown: enter them again to keep the connection working.</Alert>
      )}
      {notice !== "" && (
        <Alert tone="error" title="Not saved" mark="connection">
          {notice}
        </Alert>
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
