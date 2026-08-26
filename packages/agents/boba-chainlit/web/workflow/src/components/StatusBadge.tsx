import type { ReactElement } from "react";
import type { RunStatus, TaskStatus } from "../model/status";

type Props = {
  status: RunStatus | TaskStatus;
};

/** Кружок статуса с подписью; цвет — токен --status-<имя>. */
export function StatusBadge({ status }: Props): ReactElement {
  const style = { "--status-color": `var(--status-${status})` } as React.CSSProperties;
  return (
    <span className="badge" data-status={status} style={style}>
      {status}
    </span>
  );
}
