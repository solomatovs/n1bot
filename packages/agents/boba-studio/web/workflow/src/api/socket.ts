import { io, type Socket } from "socket.io-client";

import type { PageUrls } from "../config";
import {
  RunSnapshotSchema,
  StreamEventSchema,
  UserEventSchema,
  withKnownResults,
  type RunSnapshot,
  type StreamEvent,
  type UserEvent,
} from "../model/workflow";

/** События namespace /workflow — те же имена, что у WorkflowSocketEvent на сервере. */
const NAMESPACE = "/workflow";

const EVENT = {
  subscribe: "subscribe",
  unsubscribe: "unsubscribe",
  runState: "run_state",
  refused: "refused",
  busState: "bus_state",
  userEvent: "user_event",
  streamEvent: "stream_event",
} as const;

/** Состояние слушателя шины на сервере — те же значения, что у ListenerState. */
export const BUS_STATE = {
  stopped: "stopped",
  connecting: "connecting",
  listening: "listening",
  failed: "failed",
} as const;

export type BusState = (typeof BUS_STATE)[keyof typeof BUS_STATE];

/** Что видит сокет сам по себе: подключение, связь, обрыв. */
export type LinkState = "connecting" | "connected" | "disconnected";

export type SnapshotListener = (snapshot: RunSnapshot) => void;
export type UserEventListener = (event: UserEvent) => void;
export type StreamEventListener = (event: StreamEvent) => void;
export type RefusalListener = (reason: string) => void;

/** Состояние живой связи для лампочки: сокет плюс слушатель шины на сервере;
 * degraded — сокет есть, но сервер не слушает шину, снимки не придут. */
export type SocketStatus = {
  state: LinkState | "degraded";
  detail: string;
  bus: BusState;
};

/** Сводит состояние сокета и слушателя шины в одно состояние лампочки. */
export function lampStatus(link: LinkState, linkDetail: string, bus: BusState): SocketStatus {
  if (link !== "connected") {
    return { state: link, detail: linkDetail, bus };
  }

  if (bus !== BUS_STATE.listening) {
    return { state: "degraded", detail: `server bus listener is ${bus}`, bus };
  }

  return { state: "connected", detail: linkDetail, bus };
}

export type StatusListener = (status: SocketStatus) => void;

/** Один сокет на приложение: живые снимки запусков и состояние связи для лампочки. */
export class RunSocket {
  private readonly socket: Socket;
  private link: LinkState = "connecting";
  private linkDetail = "connecting";
  private bus: BusState = BUS_STATE.connecting;
  private current: SocketStatus = lampStatus("connecting", "connecting", BUS_STATE.connecting);
  private readonly listeners = new Set<StatusListener>();

  constructor(urls: PageUrls) {
    this.socket = io(NAMESPACE, {
      path: urls.socketPath,
      withCredentials: true,
      transports: ["websocket"],
    });
    this.socket.on("connect", () => {
      this.setLink("connected", "live updates on");
    });
    this.socket.on("disconnect", (reason: string) => {
      this.setLink("disconnected", `disconnected: ${reason}`);
    });
    this.socket.on("connect_error", (error: Error) => {
      this.setLink("disconnected", `connect error: ${error.message}`);
    });
    this.socket.io.on("reconnect_attempt", () => {
      this.setLink("connecting", "reconnecting");
    });
    this.socket.on(EVENT.busState, (payload: unknown) => {
      this.bus = busStateOf(payload);
      this.update(lampStatus(this.link, this.linkDetail, this.bus));
    });
  }

  private setLink(link: LinkState, detail: string): void {
    this.link = link;
    this.linkDetail = detail;
    this.update(lampStatus(link, detail, this.bus));
  }

  get status(): SocketStatus {
    return this.current;
  }

  /** Идентификатор сокета этой вкладки; пусто, пока связи нет. */
  get id(): string {
    return this.socket.id ?? "";
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

  /** События лент пользователя (запуски, workflow, соединения) с любого инстанса;
   * незнакомое событие пропускается — фронт старее сервера. */
  onUser(listener: UserEventListener): () => void {
    const deliver = (payload: unknown): void => {
      const parsed = UserEventSchema.safeParse(payload);
      if (parsed.success) {
        listener(parsed.data);
      }
    };

    this.socket.on(EVENT.userEvent, deliver);
    return () => {
      this.socket.off(EVENT.userEvent, deliver);
    };
  }

  /** События журнала стадий запуска runId: рост канала или его закрытие;
   * отписка — возвращаемая функция. */
  onStream(runId: string, listener: StreamEventListener): () => void {
    const deliver = (payload: unknown): void => {
      const event = streamEventOf(payload);
      if (event !== null && event.run_id === runId) {
        listener(event);
      }
    };

    this.socket.on(EVENT.streamEvent, deliver);
    return () => {
      this.socket.off(EVENT.streamEvent, deliver);
    };
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

/** Разбирает событие bus_state; незнакомое значение считается сбоем слушателя. */
/** Событие журнала из payload сокета; незнакомая форма — null: фронт старее сервера. */
export function streamEventOf(payload: unknown): StreamEvent | null {
  const parsed = StreamEventSchema.safeParse(payload);
  if (!parsed.success) {
    return null;
  }

  return parsed.data;
}

export function busStateOf(payload: unknown): BusState {
  if (typeof payload === "object" && payload !== null && "listener" in payload) {
    const value = String(payload.listener);
    for (const known of Object.values(BUS_STATE)) {
      if (known === value) {
        return known;
      }
    }
  }

  return BUS_STATE.failed;
}

function reasonOf(payload: unknown): string {
  if (typeof payload === "object" && payload !== null && "reason" in payload) {
    return String(payload.reason);
  }

  return "subscription refused";
}
