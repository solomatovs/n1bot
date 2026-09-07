import { Plus } from "lucide-react";
import { useState, type FormEvent, type ReactElement } from "react";

import { ApiError, type CatalogApi } from "../../api/client";
import type { Draft } from "../../model/catalog";
import { Button, Dialog, Field, Form, Input, Note, Toolbar, useToast } from "../../ui";

type Props = {
  api: CatalogApi;
  onCreated: (draft: Draft) => void;
  onClose: () => void;
};

/** Новый процесс начинается черновиком: имя черновика станет именем процесса
 * при публикации; занятое имя всплывёт при публикации, а не здесь. */
export function NewProcessDialog({ api, onCreated, onClose }: Props): ReactElement {
  const toast = useToast();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    setBusy(true);
    api
      .createDraft(null, name.trim())
      .then(onCreated)
      .catch((error: unknown) => {
        setBusy(false);
        toast(describe(error), "error");
      });
  };

  return (
    <Dialog title="new process" mark="new-process" onClose={onClose}>
      <Form onSubmit={submit} mark="new-process-form">
        <Note>A new process starts as your draft; publishing it creates the process under this name.</Note>
        <Field label="name" required>
          <Input
            fill
            mono
            autoFocus
            aria-label="process name"
            value={name}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </Field>
        <Toolbar>
          <Button type="submit" tone="primary" icon={Plus} disabled={busy || name.trim() === ""}>
            create
          </Button>
          <Button tone="ghost" onClick={onClose}>
            cancel
          </Button>
        </Toolbar>
      </Form>
    </Dialog>
  );
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail;
  }

  return String(error);
}
