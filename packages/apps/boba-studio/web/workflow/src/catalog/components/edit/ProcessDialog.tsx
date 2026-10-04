import { Save, Trash2 } from "lucide-react";
import { useState, type FormEvent, type ReactElement } from "react";

import { ApiError } from "../../../api/transport";
import type { CatalogApi } from "../../api/client";
import type { Process } from "../../model/catalog";
import { Alert, Button, Dialog, Field, Form, Input, TextArea, Toolbar, ToolbarSpacer, useToast } from "../../../ui";

type Props = {
  api: CatalogApi;
  process: Process;
  /** Владелец: может удалить процесс целиком. */
  owned: boolean;
  onSaved: () => void;
  onDeleted: () => void;
  onClose: () => void;
};

/** Свойства процесса: имя и описание; владельцу — удаление со всеми
 * версиями, черновиками и ссылками. */
export function ProcessDialog({ api, process, owned, onSaved, onDeleted, onClose }: Props): ReactElement {
  const toast = useToast();
  const [name, setName] = useState(process.name);
  const [description, setDescription] = useState(process.description);
  const [deleting, setDeleting] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    setBusy(true);
    api
      .updateProcess(process.id, { name: name.trim(), description: description.trim() })
      .then(() => {
        setBusy(false);
        toast("process saved", "success");
        onSaved();
      })
      .catch((error: unknown) => {
        setBusy(false);
        toast(ApiError.describe(error), "error");
      });
  };

  const remove = (): void => {
    setBusy(true);
    api
      .deleteProcess(process.id)
      .then(() => {
        toast("process deleted", "success");
        onDeleted();
      })
      .catch((error: unknown) => {
        setBusy(false);
        toast(ApiError.describe(error), "error");
      });
  };

  return (
    <Dialog title="process" mark="process-settings" onClose={onClose}>
      <Form onSubmit={submit} mark="process-form">
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
        <Field label="description">
          <TextArea
            fill
            rows={3}
            aria-label="process description"
            value={description}
            onChange={(event) => {
              setDescription(event.target.value);
            }}
          />
        </Field>
        {deleting && (
          <Alert tone="error" title="delete the process?" mark="process-delete">
            “{process.name}” with all its versions, drafts and share links will be deleted.
          </Alert>
        )}
        <Toolbar>
          <Button type="submit" tone="primary" icon={Save} disabled={busy || name.trim() === ""}>
            save
          </Button>
          <Button tone="ghost" onClick={onClose}>
            cancel
          </Button>
          {owned && (
            <>
              <ToolbarSpacer />
              {deleting ? (
                <Button tone="danger" disabled={busy} onClick={remove} data-testid="delete-process-confirm">
                  delete the process
                </Button>
              ) : (
                <Button
                  tone="danger"
                  icon={Trash2}
                  onClick={() => {
                    setDeleting(true);
                  }}
                  data-testid="delete-process"
                >
                  delete
                </Button>
              )}
            </>
          )}
        </Toolbar>
      </Form>
    </Dialog>
  );
}

