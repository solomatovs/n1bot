import { RefreshCw } from "lucide-react";
import { useState, type FormEvent, type ReactElement } from "react";

import type { SyncScope } from "../../model/catalog";
import { Button, Dialog, Field, Form, Input, Toolbar } from "../../../ui";

type Props = {
  connectionName: string;
  onStart: (scope: SyncScope) => void;
  onClose: () => void;
};

/** Схемы из строки через запятую; пусто — все несистемные. */
export function parseSchemas(raw: string): string[] {
  return raw
    .split(",")
    .map((piece) => piece.trim())
    .filter((piece) => piece !== "");
}

/** Диалог синхронизации подключения: какие схемы снимать. */
export function SyncDialog({ connectionName, onStart, onClose }: Props): ReactElement {
  const [schemas, setSchemas] = useState("");

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    onStart({ schemas: parseSchemas(schemas) });
  };

  return (
    <Dialog title={`sync ${connectionName}`} mark="connection-sync" onClose={onClose}>
      <Form onSubmit={submit}>
        <Field label="schemas" hint="comma-separated; empty takes every non-system schema">
          <Input
            mono
            fill
            value={schemas}
            aria-label="sync schemas"
            placeholder="public, etl"
            onChange={(event) => {
              setSchemas(event.target.value);
            }}
          />
        </Field>
        <Toolbar>
          <Button tone="primary" type="submit" icon={RefreshCw} data-testid="start-sync">
            start sync
          </Button>
          <Button tone="ghost" onClick={onClose}>
            cancel
          </Button>
        </Toolbar>
      </Form>
    </Dialog>
  );
}
