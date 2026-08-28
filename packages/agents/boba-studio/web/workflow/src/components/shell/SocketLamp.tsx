import { type ReactElement, useEffect, useState } from "react";

import type { SocketStatus } from "../../api/socket";
import { useServices } from "../../app";

/** Лампочка связи: состояние socket.io живых снимков; подробность — в подсказке. */
export function SocketLamp(): ReactElement {
  const { socket } = useServices();
  const [status, setStatus] = useState<SocketStatus>(socket.status);

  useEffect(() => socket.onStatus(setStatus), [socket]);

  return (
    <span
      className={`lamp lamp--${status.state}`}
      role="status"
      aria-label={`live updates: ${status.state}`}
      title={status.detail}
      data-socket={status.state}
    />
  );
}
