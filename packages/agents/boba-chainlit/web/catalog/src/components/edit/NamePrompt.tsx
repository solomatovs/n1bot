import { useState, type FormEvent, type ReactElement } from "react";

import { Button, Field, Input } from "../../ui";
import { Dialog } from "./Dialog";

type Props = {
  title: string;
  mark: string;
  label: string;
  initial: string;
  onSubmit: (name: string) => void;
  onClose: () => void;
};

/** Одно имя: новый слой, новый набор, переименование слоя. */
export function NamePrompt({ title, mark, label, initial, onSubmit, onClose }: Props): ReactElement {
  const [name, setName] = useState(initial);
  const trimmed = name.trim();

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    if (trimmed === "") {
      return;
    }

    onSubmit(trimmed);
  };

  return (
    <Dialog title={title} mark={mark} onClose={onClose}>
      <form className="form" onSubmit={submit}>
        <Field label={label} required>
          <Input
            mono
            autoFocus
            value={name}
            aria-label={label}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </Field>
        <div className="form__actions">
          <Button tone="primary" type="submit" disabled={trimmed === ""}>
            save
          </Button>
          <Button tone="ghost" onClick={onClose}>
            cancel
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
