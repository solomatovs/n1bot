import { Plus } from "lucide-react";
import { useState, type FormEvent, type ReactElement } from "react";
import { Link } from "react-router-dom";

import type { Access, Draft } from "../../model/catalog";
import { Button, Chip, Input } from "../../ui";
import { Dialog } from "./Dialog";

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
      <ul className="index__list" data-testid="drafts-list">
        {drafts.length === 0 && <li className="choices__empty">no open drafts</li>}
        {drafts.map((draft) => (
          <li key={draft.id} data-draft={draft.name}>
            <Link to={`/drafts/${draft.id}`} className="index__link">
              {draft.name}
            </Link>
            <Chip tone="muted">over v{draft.base_version}</Chip>
            <Chip tone={draft.created_by === access.user_id ? "draft" : "muted"}>
              {draft.created_by === access.user_id ? "yours" : "someone else's"}
            </Chip>
          </li>
        ))}
      </ul>
      {access.can_edit && (
        <form className="index__new" onSubmit={submit} data-testid="new-draft">
          <Input
            mono
            autoFocus
            placeholder="new draft name"
            aria-label="new draft name"
            value={name}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
          <Button size="tiny" tone="primary" type="submit" icon={Plus} disabled={name.trim() === ""}>
            draft
          </Button>
        </form>
      )}
    </Dialog>
  );
}
