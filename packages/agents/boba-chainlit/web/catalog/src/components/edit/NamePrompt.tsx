import { useState, type FormEvent, type ReactElement } from "react";

import { Button, Dialog, Field, Form, Input, Toolbar } from "../../ui";

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
      <Form onSubmit={submit}>
        <Field label={label} required>
          <Input
            mono
            fill
            autoFocus
            value={name}
            aria-label={label}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </Field>
        <Toolbar>
          <Button tone="primary" type="submit" disabled={trimmed === ""}>
            save
          </Button>
          <Button tone="ghost" onClick={onClose}>
            cancel
          </Button>
        </Toolbar>
      </Form>
    </Dialog>
  );
}
