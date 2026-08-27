import type { ReactElement } from "react";

import type { RunStatus, TaskStatus } from "../model/status";

type Props = {
  status: RunStatus | TaskStatus;
};

/** Пилюля статуса с точкой цвета токена --status-<имя>. */
export function StatusPill({ status }: Props): ReactElement {
  const style = { "--status-color": `var(--status-${status})` } as React.CSSProperties;
  return (
    <span className="pill" data-status={status} style={style}>
      {status}
    </span>
  );
}
