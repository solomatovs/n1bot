import { type ReactElement, useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../../api/client";
import { useServices } from "../../app";
import type { ChannelView, StreamEvent, StreamSlice } from "../../model/workflow";
import { Alert } from "../Alert";
import { errorText } from "../Async";
import { Segmented } from "../Segmented";

type Props = {
  runId: string;
  callId: string;
};

/** Вывод стадии из журнала: вкладки каналов и текст окнами от начала. О новых записях
 * и закрытии канала сообщает шина (socket.io `stream_event`), опроса нет. */
export function OutputPanel({ runId, callId }: Props): ReactElement | null {
  const { api, socket } = useServices();
  const [channels, setChannels] = useState<ChannelView[]>([]);
  const [channel, setChannel] = useState("");
  const [text, setText] = useState("");
  const [tail, setTail] = useState<StreamSlice | null>(null);
  const [error, setError] = useState("");
  const [poke, setPoke] = useState(0);
  const last = useRef<StreamEvent | null>(null);
  const offset = useRef(0);
  const reading = useRef<object>({});
  const pulling = useRef(false);
  const again = useRef(false);
  const wanted = (): boolean => again.current;

  const askChannels = useCallback((): void => {
    void api.streamChannels(runId, callId).then(
      (found) => {
        setChannels(found);
        setChannel((current) => (current === "" ? (found[0]?.name ?? "") : current));
      },
      (failure: unknown) => {
        if (!(failure instanceof ApiError && failure.status === 404)) {
          setError(errorText(failure));
        }
      },
    );
  }, [api, runId, callId]);

  // при монтировании — какие каналы уже записаны
  useEffect(() => {
    askChannels();
  }, [askChannels]);

  // события шины по этому вызову: рост канала или его закрытие
  useEffect(
    () =>
      socket.onStream(runId, (event) => {
        if (event.call_id !== callId) return;
        last.current = event;
        setPoke((count) => count + 1);
      }),
    [socket, runId, callId],
  );

  // после обрыва сокета события потеряны: по восстановлению связи перечитать хвост
  useEffect(
    () =>
      socket.onStatus((status) => {
        if (status.state === "connected") setPoke((count) => count + 1);
      }),
    [socket],
  );

  // окна читаются по одному: параллельные запросы удвоили бы текст
  const pull = useCallback(async (): Promise<void> => {
    if (channel === "") return;
    if (pulling.current) {
      again.current = true;
      return;
    }

    const mine = reading.current;
    pulling.current = true;
    try {
      do {
        again.current = false;
        const slice = await api.streamWindow(runId, callId, channel, offset.current);
        if (reading.current !== mine) return;
        if (slice.end > offset.current) {
          setText((current) => current + slice.text);
          offset.current = slice.end;
        }
        setTail(slice);
      } while (wanted());
    } catch (failure: unknown) {
      setError(errorText(failure));
    } finally {
      pulling.current = false;
    }
  }, [api, runId, callId, channel]);

  // смена канала — чтение с начала
  useEffect(() => {
    reading.current = {};
    offset.current = 0;
    setText("");
    setTail(null);
    setError("");
    void pull();
  }, [channel, pull]);

  // сигнал шины: новый канал — перечитать вкладки; текущий дорос или закрыт — дочитать
  useEffect(() => {
    if (poke === 0) return;
    const event = last.current;
    if (event === null) {
      void pull();
      return;
    }

    const known = channels.some((view) => view.name === event.channel);
    if (!known) askChannels();
    if (event.channel !== channel) return;
    if (event.size > offset.current || event.closed) void pull();
  }, [poke, channels, channel, askChannels, pull]);

  if (channels.length === 0 && error === "") {
    return null;
  }

  return (
    <section className="output" aria-label="output">
      <div className="output__head">
        <span className="eyebrow">output</span>
        <Segmented
          options={channels.map((view) => ({ value: view.name, label: view.label }))}
          value={channel}
          onChange={setChannel}
          label="output channel"
        />
        {tail !== null && (
          <span className="output__meta">
            {tail.size} B{tail.closed ? "" : " · live"}
            {tail.note !== "" && ` · ${tail.note}`}
          </span>
        )}
      </div>
      {error !== "" && <Alert tone="error">{error}</Alert>}
      <pre className="output__text">{text}</pre>
    </section>
  );
}
