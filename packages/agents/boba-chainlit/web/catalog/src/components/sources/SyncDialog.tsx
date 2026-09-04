import { RefreshCw } from "lucide-react";
import { useState, type FormEvent, type ReactElement } from "react";

import type { ConnectionView, SourceConnection, SyncScope } from "../../model/catalog";
import { Alert, Button, Dialog, Field, Form, Input, Row, Select, Toolbar } from "../../ui";
import { connectionLabel } from "./ConnectionsDialog";

type Props = {
  sourceName: string;
  bound: SourceConnection[];
  directory: ConnectionView[];
  onStart: (connectionId: string, scope: SyncScope) => void;
  onClose: () => void;
};

/** Пределы охвата синхронизации: зеркало SyncScope сервиса. */
const SCOPE_LIMITS = {
  batchMin: 1,
  batchMax: 10_000,
  batchDefault: 200,
  pauseMin: 0,
  pauseMax: 60_000,
};

/** Схемы из строки через запятую; пусто — все несистемные. */
export function parseSchemas(raw: string): string[] {
  return raw
    .split(",")
    .map((piece) => piece.trim())
    .filter((piece) => piece !== "");
}

/** Диалог синхронизации: привязанное подключение, схемы, размер порции и
 * пауза между заходами инструмента в каталог базы. */
export function SyncDialog({ sourceName, bound, directory, onStart, onClose }: Props): ReactElement {
  const usable = bound.filter((item) => directory.some((entry) => entry.id === item.connection_id));
  const [connection, setConnection] = useState<string>("");
  const [schemas, setSchemas] = useState("");
  const [batchSize, setBatchSize] = useState(String(SCOPE_LIMITS.batchDefault));
  const [pauseMs, setPauseMs] = useState("0");
  const chosen = connection !== "" ? connection : (usable[0]?.connection_id ?? "");

  const batch = Number(batchSize);
  const pause = Number(pauseMs);
  const batchOk = Number.isInteger(batch) && batch >= SCOPE_LIMITS.batchMin && batch <= SCOPE_LIMITS.batchMax;
  const pauseOk = Number.isInteger(pause) && pause >= SCOPE_LIMITS.pauseMin && pause <= SCOPE_LIMITS.pauseMax;
  const ready = chosen !== "" && batchOk && pauseOk;

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    if (!ready) {
      return;
    }

    onStart(chosen, {
      schemas: parseSchemas(schemas),
      batch_size: batch,
      pause_ms: pause,
    });
  };

  return (
    <Dialog title={`sync ${sourceName}`} mark="source-sync" onClose={onClose}>
      <Form onSubmit={submit}>
        {usable.length === 0 && (
          <Alert tone="info" mark="sync-no-connection">
            bind a connection you have access to before syncing
          </Alert>
        )}
        <Field label="connection" required>
          <Select
            fill
            aria-label="sync connection"
            value={chosen}
            disabled={usable.length === 0}
            onChange={(event) => {
              setConnection(event.target.value);
            }}
          >
            {usable.map((item) => (
              <option key={item.connection_id} value={item.connection_id}>
                {connectionLabel(item.connection_id, directory)}
              </option>
            ))}
          </Select>
        </Field>
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
        <Row wrap>
          <Field
            label="batch size"
            invalid={!batchOk}
            issue={batchOk ? undefined : `${SCOPE_LIMITS.batchMin}…${SCOPE_LIMITS.batchMax}`}
          >
            <Input
              mono
              narrow
              type="number"
              value={batchSize}
              aria-label="sync batch size"
              min={SCOPE_LIMITS.batchMin}
              max={SCOPE_LIMITS.batchMax}
              onChange={(event) => {
                setBatchSize(event.target.value);
              }}
            />
          </Field>
          <Field
            label="pause, ms"
            invalid={!pauseOk}
            issue={pauseOk ? undefined : `${SCOPE_LIMITS.pauseMin}…${SCOPE_LIMITS.pauseMax}`}
          >
            <Input
              mono
              narrow
              type="number"
              value={pauseMs}
              aria-label="sync pause"
              min={SCOPE_LIMITS.pauseMin}
              max={SCOPE_LIMITS.pauseMax}
              onChange={(event) => {
                setPauseMs(event.target.value);
              }}
            />
          </Field>
        </Row>
        <Toolbar>
          <Button tone="primary" type="submit" icon={RefreshCw} disabled={!ready} data-testid="start-sync">
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
