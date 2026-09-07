import { RefreshCw } from "lucide-react";
import { useState, type FormEvent, type ReactElement } from "react";

import type { SyncScope } from "../../model/catalog";
import { Button, Dialog, Field, Form, Input, Row, Toolbar } from "../../ui";

type Props = {
  connectionName: string;
  onStart: (scope: SyncScope) => void;
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

/** Диалог синхронизации подключения: схемы, размер порции и пауза между
 * заходами инструмента в каталог базы. */
export function SyncDialog({ connectionName, onStart, onClose }: Props): ReactElement {
  const [schemas, setSchemas] = useState("");
  const [batchSize, setBatchSize] = useState(String(SCOPE_LIMITS.batchDefault));
  const [pauseMs, setPauseMs] = useState("0");

  const batch = Number(batchSize);
  const pause = Number(pauseMs);
  const batchOk = Number.isInteger(batch) && batch >= SCOPE_LIMITS.batchMin && batch <= SCOPE_LIMITS.batchMax;
  const pauseOk = Number.isInteger(pause) && pause >= SCOPE_LIMITS.pauseMin && pause <= SCOPE_LIMITS.pauseMax;
  const ready = batchOk && pauseOk;

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    if (!ready) {
      return;
    }

    onStart({
      schemas: parseSchemas(schemas),
      batch_size: batch,
      pause_ms: pause,
    });
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
