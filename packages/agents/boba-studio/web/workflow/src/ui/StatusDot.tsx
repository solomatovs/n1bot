import type { CSSProperties, ReactElement } from "react";

import "./StatusDot.css";

type Props = {
  /** Статус запуска — красит точку токеном --status-<статус>. */
  status?: string | undefined;
};

/** Цветовая точка строки списка. Единственное место с классом `item__dot`
 * и его inline-переменной цвета. */
export function StatusDot({ status }: Props): ReactElement {
  let paint = "var(--signal)";
  if (status !== undefined) {
    paint = `var(--status-${status})`;
  }

  return (
    <span
      className="item__dot"
      data-status={status}
      style={{ "--status-color": paint } as CSSProperties}
    />
  );
}
