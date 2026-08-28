import { io, type Socket } from "socket.io-client";

import type { PageUrls } from "../config";
import { RunSnapshotSchema, withKnownResults, type RunSnapshot } from "../model/workflow";

/** События namespace /workflow — те же имена, что у WorkflowSocketEvent на сервере. */
const NAMESPACE = "/workflow";

const EVENT = {
  subscribe: "subscribe",
  unsubscribe: "unsubscribe",
  runState: "run_state",
  refused: "refused",
} as const;

export type SnapshotListener = (snapshot: RunSnapshot) => void;
export type RefusalListener = (reason: string) => void;

/** Состояние сокета для индикатора: подключается, подключён, оборван (с причиной). */
export type SocketStatus = {
  state: "connecting" | "connected" | "disconnected";
  detail: string;
};

export type StatusListener = (status: SocketStatus) => void;

/** Один сокет на приложение: живые снимки запусков и состояние связи для лампочки. */
export class RunSocket {
  private readonly socket: Socket;
  private current: SocketStatus = { state: "connecting", detail: "connecting" };
  private readonly listeners = new Set<StatusListener>();

  constructor(urls: PageUrls) {
    this.socket = io(NAMESPACE, {
      path: urls.socketPath,
      withCredentials: true,
      transports: ["websocket"],
    });
    this.socket.on("connect", () => {
      this.update({ state: "connected", detail: "live updates on" });
    });
    this.socket.on("disconnect", (reason: string) => {
      this.update({ state: "disconnected", detail: `disconnected: ${reason}` });
    });
    this.socket.on("connect_error", (error: Error) => {
      this.update({ state: "disconnected", detail: `connect error: ${error.message}` });
    });
    this.socket.io.on("reconnect_attempt", () => {
      this.update({ state: "connecting", detail: "reconnecting" });
    });
  }

  get status(): SocketStatus {
    return this.current;
  }

  /** Слушатель состояния; отписка — возвращаемая функция. */
  onStatus(listener: StatusListener): () => void {
    this.listeners.add(listener);
    listener(this.current);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private update(status: SocketStatus): void {
    this.current = status;
    for (const listener of this.listeners) {
      listener(status);
    }
  }

  subscribe(runId: string, onSnapshot: SnapshotListener, onRefused: RefusalListener): () => void {
    const deliver = (payload: unknown): void => {
      const parsed = RunSnapshotSchema.safeParse(withKnownResults(payload));
      if (!parsed.success) {
        onRefused(`bad snapshot: ${parsed.error.message}`);
        return;
      }

      if (parsed.data.run_id === runId) {
        onSnapshot(parsed.data);
      }
    };

    const refuse = (payload: unknown): void => {
      onRefused(reasonOf(payload));
    };

    const ask = (): void => {
      this.socket.emit(EVENT.subscribe, { run_id: runId });
    };

    this.socket.on(EVENT.runState, deliver);
    this.socket.on(EVENT.refused, refuse);
    // реконнект переподписывает сам: комната живёт на сокете, а не на странице
    this.socket.on("connect", ask);
    if (this.socket.connected) {
      ask();
    }

    return () => {
      this.socket.emit(EVENT.unsubscribe, { run_id: runId });
      this.socket.off(EVENT.runState, deliver);
      this.socket.off(EVENT.refused, refuse);
      this.socket.off("connect", ask);
    };
  }

  close(): void {
    this.socket.close();
  }
}

function reasonOf(payload: unknown): string {
  if (typeof payload === "object" && payload !== null && "reason" in payload) {
    return String(payload.reason);
  }

  return "subscription refused";
}
