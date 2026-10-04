import { XCircle } from "lucide-react";
import type { ReactElement } from "react";

import { formatStamp } from "../../../model/time";
import type { Sync, SyncStatus, UpgradeRun } from "../../model/catalog";
import { Alert, Button } from "../../../ui";

/** Одна полоса на все задачи каталога — синхронизацию и upgrade: ход с
 * отменой, итог, сбой. Страницы дают только тексты; `mark` и `progress`
 * различают задачи в тестах. */
type Props = {
  status: SyncStatus;
  /** Метка полосы (data-notice), строки хода и кнопки отмены (data-testid). */
  mark: string;
  progress: string;
  cancel: string;
  running: string;
  done: string;
  /** Заголовок сбоя: статус и время. */
  failed: string;
  error: string;
  canCancel: boolean;
  onCancel: () => void;
};

export function TaskBar({
  status,
  mark,
  progress,
  cancel,
  running,
  done,
  failed,
  error,
  canCancel,
  onCancel,
}: Props): ReactElement {
  if (status === "running") {
    return (
      <Alert tone="info" mark={mark}>
        <span data-testid={progress} data-status={status}>
          {running}
        </span>{" "}
        {canCancel && (
          <Button size="sm" tone="ghost" icon={XCircle} onClick={onCancel} data-testid={cancel}>
            cancel
          </Button>
        )}
      </Alert>
    );
  }

  if (status === "done") {
    return (
      <Alert tone="ok" mark={mark}>
        <span data-testid={progress} data-status={status}>
          {done}
        </span>
      </Alert>
    );
  }

  return (
    <Alert tone="error" mark={mark} title={failed}>
      <span data-testid={progress} data-status={status}>
        {error}
      </span>
    </Alert>
  );
}

type SyncBarProps = { sync: Sync; canCancel: boolean; onCancel: () => void };

/** Полоса последней синхронизации подключения. */
export function SyncBar({ sync, canCancel, onCancel }: SyncBarProps): ReactElement {
  const started = formatStamp(sync.started_at);
  let total = "?";
  if (sync.objects_total !== null) {
    total = String(sync.objects_total);
  }

  let error = "";
  if (sync.error !== null) {
    error = sync.error;
  }

  return (
    <TaskBar
      status={sync.status}
      mark="sync-status"
      progress="sync-progress"
      cancel="cancel-sync"
      running={`syncing ${sync.connection_name}: ${sync.objects_done} / ${total} objects`}
      done={`synced v${sync.version} at ${started}: ${sync.objects_done} objects`}
      failed={`sync ${sync.status} at ${started}`}
      error={error}
      canCancel={canCancel}
      onCancel={onCancel}
    />
  );
}

type UpgradeBarProps = { run: UpgradeRun; canCancel: boolean; onCancel: () => void };

/** Полоса последнего запуска upgrade: сколько процессов пройдено, итог по
 * переведённым и остановленным. */
export function UpgradeBar({ run, canCancel, onCancel }: UpgradeBarProps): ReactElement {
  const started = formatStamp(run.started_at);
  let what = "all lagging processes";
  if (run.target === "process") {
    what = "the process";
  }
  if (run.target === "draft") {
    what = "the draft";
  }

  let error = "";
  if (run.error !== null) {
    error = run.error;
  }

  return (
    <TaskBar
      status={run.status}
      mark="upgrade-status"
      progress="upgrade-progress"
      cancel="cancel-upgrade"
      running={`upgrading ${what}: ${run.done} / ${run.total}`}
      done={`upgrade of ${what} at ${started}: ${run.moved} moved, ${run.blocked} blocked`}
      failed={`upgrade ${run.status} at ${started}`}
      error={error}
      canCancel={canCancel}
      onCancel={onCancel}
    />
  );
}
