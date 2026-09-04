import { Plus } from "lucide-react";
import { useState, type FormEvent, type ReactElement } from "react";

import type { Access, Draft } from "../../model/catalog";
import { Button, Chip, Dialog, Form, Input, List, ListAside, ListName, ListRow } from "../../ui";

type Props = {
  access: Access;
  drafts: Draft[];
  onCreate: (name: string) => void;
  onClose: () => void;
};

/** Вход в правки: свои открытые черновики — продолжить, чужие — посмотреть,
 * форма нового черновика. */
export function DraftsDialog({ access, drafts, onCreate, onClose }: Props): ReactElement {
  const [name, setName] = useState("");

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    const trimmed = name.trim();
    if (trimmed !== "") {
      onCreate(trimmed);
    }
  };

  return (
    <Dialog title="edit the process" mark="drafts" onClose={onClose}>
      <List kind="spaced" mark="drafts-list" empty="no open drafts">
        {drafts.map((draft) => (
          <ListRow key={draft.id} data-draft={draft.name}>
            <ListName to={`/drafts/${draft.id}`}>{draft.name}</ListName>
            <ListAside>
              <Chip tone="muted">over v{draft.base_version}</Chip>
              <Chip tone={draft.created_by === access.user_id ? "draft" : "muted"}>
                {draft.created_by === access.user_id ? "yours" : "someone else's"}
              </Chip>
            </ListAside>
          </ListRow>
        ))}
      </List>
      {access.can_edit && (
        <Form inline onSubmit={submit} mark="new-draft">
          <Input
            mono
            fill
            autoFocus
            placeholder="new draft name"
            aria-label="new draft name"
            value={name}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
          <Button tone="primary" type="submit" icon={Plus} disabled={name.trim() === ""}>
            draft
          </Button>
        </Form>
      )}
    </Dialog>
  );
}
