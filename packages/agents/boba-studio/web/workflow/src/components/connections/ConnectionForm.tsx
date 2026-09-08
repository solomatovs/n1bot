import { PlugZap, Save, Trash2 } from "lucide-react";
import { useCallback, useState, type FormEvent, type ReactElement } from "react";

import { ApiError } from "../../api/transport";
import { useServices } from "../../services";
import type { ConnectionView, ProbeResult } from "../../model/account";
import { type SchemaDoc, withoutMaskedSecrets } from "../../model/schema";
import { Alert, Button, Field, Form, Input, Toolbar, ToolbarSpacer } from "../../ui";
import { SchemaNode } from "./SchemaForm";

type Props = {
  doc: SchemaDoc;
  /** Существующая строка — правка (общая — только чтение); null — новая. */
  row: ConnectionView | null;
  onSaved: (saved: ConnectionView) => void;
  /** Кнопка удаления в подвале формы: страница учётной записи; без неё
   * удаление живёт в списке (каталог). */
  onRemoved?: (() => void) | undefined;
  /** Кнопка закрытия в подвале: форма в диалоге. */
  onClose?: (() => void) | undefined;
};

/** Форма соединения по схеме api: имя и профиль с любой вложенностью,
 * пробное соединение, сохранение. Секреты сохранённой строки не
 * показываются и вводятся заново. Одна и та же для вкладки учётной записи и
 * диалога каталога: ошибки полей 422 ложатся под поля. */
export function ConnectionForm({ doc, row, onSaved, onRemoved, onClose }: Props): ReactElement {
  const { api } = useServices();
  const [name, setName] = useState(row?.name ?? "");
  const [profile, setProfile] = useState<unknown>(() => {
    if (row?.profile == null) {
      return doc.defaults(doc.root);
    }

    return withoutMaskedSecrets(row.profile);
  });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [issues, setIssues] = useState<ReadonlyMap<string, string>>(new Map());
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const readonly = row !== null && !row.mine;

  const fail = useCallback(
    (failure: unknown) => {
      setBusy(false);
      if (!(failure instanceof ApiError)) {
        setNotice(ApiError.describe(failure));
        return;
      }

      // ошибки полей — под полями, остальное (и ошибки без поля) — в общем сообщении
      const byField = new Map<string, string>();
      const general: string[] = [];
      for (const issue of failure.issues) {
        let path = issue.loc.join(".");
        if (issue.loc[0] === "profile") {
          path = doc.formPath("profile", issue.loc.slice(1));
        }
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

      if (byField.size > 0) {
        setNotice(`check ${byField.size} field(s) marked below`);
        return;
      }

      setNotice(failure.detail);
    },
    [doc],
  );

  const begin = (): void => {
    setBusy(true);
    setNotice("");
    setIssues(new Map());
  };

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    begin();
    const body = { name: name.trim(), profile: profile as Record<string, unknown> };
    let request = api.createConnection(body);
    if (row !== null) {
      request = api.replaceConnection(row.id, body);
    }
    request.then((saved) => {
      setBusy(false);
      onSaved(saved);
    }, fail);
  };

  // общее — по сохранённой строке (секреты у сервера), своё — по черновику формы
  const check = (): void => {
    begin();
    setProbe(null);
    let request = api.checkConnection(profile as Record<string, unknown>);
    if (readonly) {
      request = api.checkStoredConnection(row.id);
    }
    request.then((result) => {
      setBusy(false);
      setProbe(result);
    }, fail);
  };

  const remove = (): void => {
    if (row === null || onRemoved === undefined) {
      return;
    }

    begin();
    api.removeConnection(row.id).then(() => {
      setBusy(false);
      onRemoved();
    }, fail);
  };

  let closeLabel = "cancel";
  if (readonly) {
    closeLabel = "close";
  }

  return (
    <Form onSubmit={submit} mark="connection-form">
      {readonly && <Alert tone="info">Shared connection: read-only</Alert>}
      <Field label="name" required>
        <Input
          fill
          mono
          aria-label="connection name"
          value={name}
          disabled={readonly}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </Field>
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
        <Alert tone="error" title="not saved" mark="connection-error">
          {notice}
        </Alert>
      )}
      {probe !== null && <ProbeAlert probe={probe} />}
      <Toolbar>
        <Button icon={PlugZap} disabled={busy} onClick={check} data-testid="check-connection">
          check
        </Button>
        <ToolbarSpacer />
        {!readonly && onRemoved !== undefined && row !== null && (
          <Button tone="danger" icon={Trash2} disabled={busy} onClick={remove} data-testid="delete-connection">
            delete
          </Button>
        )}
        {!readonly && (
          <Button
            type="submit"
            tone="primary"
            icon={Save}
            disabled={busy || name.trim() === ""}
            data-testid="save-connection"
          >
            save
          </Button>
        )}
        {onClose !== undefined && (
          <Button tone="ghost" onClick={onClose}>
            {closeLabel}
          </Button>
        )}
      </Toolbar>
    </Form>
  );
}

/** Итог пробного соединения: тон и заголовок по ok. */
function ProbeAlert({ probe }: { probe: ProbeResult }): ReactElement {
  let tone: "ok" | "error" = "error";
  let title = "connection failed";
  if (probe.ok) {
    tone = "ok";
    title = "connected";
  }

  return (
    <Alert tone={tone} title={title} mark="probe">
      {probe.message} · {probe.elapsed_ms} ms
    </Alert>
  );
}
