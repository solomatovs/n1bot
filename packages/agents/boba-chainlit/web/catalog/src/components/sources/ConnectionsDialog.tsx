import { Link2, Unlink } from "lucide-react";
import { useState, type ReactElement } from "react";

import type { ConnectionEntry, SourceConnection } from "../../model/catalog";
import {
  Alert,
  Button,
  Chip,
  Dialog,
  Field,
  IconButton,
  List,
  ListAside,
  ListName,
  ListRow,
  Note,
  Select,
  Stack,
  Toolbar,
} from "../../ui";

type Props = {
  sourceName: string;
  bound: SourceConnection[];
  directory: ConnectionEntry[];
  directoryError: string | null;
  canEdit: boolean;
  onBind: (connectionId: string) => void;
  onUnbind: (connectionId: string) => void;
  onClose: () => void;
};

/** Имя подключения по id из справочника; чужая или удалённая строка — сам id. */
export function connectionLabel(connectionId: string, directory: ConnectionEntry[]): string {
  const entry = directory.find((item) => item.id === connectionId);
  if (entry === undefined) {
    return connectionId;
  }

  return entry.name;
}

/** Привязки подключений источника: список привязанных с отвязкой и выбор
 * видимого пользователю подключения того же вида для новой привязки. */
export function ConnectionsDialog({
  sourceName,
  bound,
  directory,
  directoryError,
  canEdit,
  onBind,
  onUnbind,
  onClose,
}: Props): ReactElement {
  const boundIds = new Set(bound.map((item) => item.connection_id));
  const candidates = directory.filter((entry) => !boundIds.has(entry.id));
  const [choice, setChoice] = useState<string>("");
  const chosen = choice !== "" ? choice : (candidates[0]?.id ?? "");

  return (
    <Dialog title={`connections of ${sourceName}`} mark="source-connections" onClose={onClose}>
      <Stack>
        {directoryError !== null && <Alert tone="error">{directoryError}</Alert>}
        {bound.length === 0 ? (
          <Note mark="no-connections">no connections bound yet</Note>
        ) : (
          <List kind="spaced" mark="bound-connections">
            {bound.map((item) => (
              <ListRow key={item.connection_id} data-connection={item.connection_id}>
                <ListName>{connectionLabel(item.connection_id, directory)}</ListName>
                <ListAside>
                  {!directory.some((entry) => entry.id === item.connection_id) && (
                    <Chip tone="muted">not visible to you</Chip>
                  )}
                  {canEdit && (
                    <IconButton
                      size="sm"
                      ghost
                      aria-label={`unbind ${connectionLabel(item.connection_id, directory)}`}
                      onClick={() => {
                        onUnbind(item.connection_id);
                      }}
                    >
                      <Unlink size={14} />
                    </IconButton>
                  )}
                </ListAside>
              </ListRow>
            ))}
          </List>
        )}
        {canEdit && (
          <Field
            label="bind a connection"
            hint={candidates.length === 0 ? "every visible connection of this kind is bound" : undefined}
            group
          >
            <Field row>
              <Select
                aria-label="connection to bind"
                value={chosen}
                disabled={candidates.length === 0}
                onChange={(event) => {
                  setChoice(event.target.value);
                }}
              >
                {candidates.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.name}
                    {entry.mine ? "" : " (shared)"}
                  </option>
                ))}
              </Select>
              <Button
                tone="primary"
                icon={Link2}
                disabled={chosen === ""}
                onClick={() => {
                  onBind(chosen);
                  setChoice("");
                }}
                data-testid="bind-connection"
              >
                bind
              </Button>
            </Field>
          </Field>
        )}
        <Toolbar>
          <Button tone="ghost" onClick={onClose}>
            close
          </Button>
        </Toolbar>
      </Stack>
    </Dialog>
  );
}
