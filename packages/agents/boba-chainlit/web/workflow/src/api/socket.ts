import { io, type Socket } from "socket.io-client";

import type { PageUrls } from "../config";
import { RunSnapshotSchema, type RunSnapshot } from "../model/workflow";

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

/** Подписка страницы на живые снимки запуска; cookie входа уходит сама. */
export class RunSocket {
  private readonly socket: Socket;

  constructor(urls: PageUrls) {
    this.socket = io(NAMESPACE, {
      path: urls.socketPath,
      withCredentials: true,
      transports: ["websocket"],
    });
  }

  subscribe(runId: string, onSnapshot: SnapshotListener, onRefused: RefusalListener): () => void {
    const deliver = (payload: unknown): void => {
      const parsed = RunSnapshotSchema.safeParse(payload);
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
