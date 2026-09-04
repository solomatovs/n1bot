import { PlugZap, Save } from "lucide-react";
import { useCallback, useState, type FormEvent, type ReactElement } from "react";

import { ApiError, type CatalogApi } from "../../api/client";
import type { ConnectionView, ProbeResult } from "../../model/catalog";
import { type SchemaDoc, withoutMaskedSecrets } from "../../model/schema";
import { Alert, Button, Dialog, Field, Form, Input, Toolbar, ToolbarSpacer } from "../../ui";
import { SchemaNode } from "./SchemaForm";

type Props = {
  api: CatalogApi;
  doc: SchemaDoc;
  /** Существующая строка — правка (общая — только чтение); null — новая. */
  row: ConnectionView | null;
  onSaved: (saved: ConnectionView) => void;
  onClose: () => void;
};

/** Диалог подключения по схеме api: имя и профиль с любой вложенностью,
 * пробное соединение, сохранение. Секреты сохранённой строки не показываются
 * и вводятся заново. */
export function ConnectionDialog({ api, doc, row, onSaved, onClose }: Props): ReactElement {
  const [name, setName] = useState(row?.name ?? "");
  const [profile, setProfile] = useState<unknown>(() =>
    row?.profile == null ? doc.defaults(doc.root) : withoutMaskedSecrets(row.profile),
  );
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [issues, setIssues] = useState<ReadonlyMap<string, string>>(new Map());
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const readonly = row !== null && !row.mine;

  const fail = useCallback(
    (failure: unknown) => {
      setBusy(false);
      if (!(failure instanceof ApiError)) {
        setNotice(String(failure));
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

      setNotice(byField.size > 0 ? `check ${byField.size} field(s) marked below` : failure.detail);
    },
    [doc],
  );

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    setBusy(true);
    setNotice("");
    setIssues(new Map());
    const body = { name: name.trim(), profile: profile as Record<string, unknown> };
    const request = row === null ? api.createConnection(body) : api.replaceConnection(row.id, body);
    request.then((saved) => {
      setBusy(false);
      onSaved(saved);
    }, fail);
  };

  // общее — по сохранённой строке (секреты у сервера), своё — по черновику формы
  const check = (): void => {
    setBusy(true);
    setProbe(null);
    setNotice("");
    setIssues(new Map());
    const request = readonly
      ? api.checkStoredConnection(row.id)
      : api.checkConnection(profile as Record<string, unknown>);
    request.then((result) => {
      setBusy(false);
      setProbe(result);
    }, fail);
  };

  return (
    <Dialog title={row === null ? "new connection" : row.name} mark="connection-form" wide onClose={onClose}>
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
        {probe !== null && (
          <Alert tone={probe.ok ? "ok" : "error"} title={probe.ok ? "connected" : "connection failed"} mark="probe">
            {probe.message} · {probe.elapsed_ms} ms
          </Alert>
        )}
        <Toolbar>
          <Button icon={PlugZap} disabled={busy} onClick={check} data-testid="check-connection">
            check
          </Button>
          <ToolbarSpacer />
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
          <Button tone="ghost" onClick={onClose}>
            {readonly ? "close" : "cancel"}
          </Button>
        </Toolbar>
      </Form>
    </Dialog>
  );
}
