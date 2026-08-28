import { type ReactElement, useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../../api/client";
import { useServices } from "../../app";
import type { ChannelView, StreamSlice } from "../../model/workflow";
import { Alert } from "../Alert";
import { errorText } from "../Async";
import { Segmented } from "../Segmented";

type Props = {
  runId: string;
  callId: string;
  /** Пока стадия идёт, хвост опрашивается; после — читается один раз. */
  live: boolean;
};

const POLL_MS = 1000;

/** Вывод стадии из журнала: вкладки каналов, текст окнами от начала, живой хвост. */
export function OutputPanel({ runId, callId, live }: Props): ReactElement | null {
  const { api } = useServices();
  const [channels, setChannels] = useState<ChannelView[]>([]);
  const [channel, setChannel] = useState("");
  const [text, setText] = useState("");
  const [tail, setTail] = useState<StreamSlice | null>(null);
  const [error, setError] = useState("");
  const offset = useRef(0);

  // каналы появляются, когда стадия начала писать: у живой стадии спрашиваем повторно
  useEffect(() => {
    let alive = true;
    const ask = (): void => {
      void api.streamChannels(runId, callId).then(
        (found) => {
          if (!alive) return;
          setChannels(found);
          setChannel((current) => (current === "" ? (found[0]?.name ?? "") : current));
        },
        (failure: unknown) => {
          if (alive && !(failure instanceof ApiError && failure.status === 404)) {
            setError(errorText(failure));
          }
        },
      );
    };
    ask();
    if (!live) {
      return () => {
        alive = false;
      };
    }

    const timer = window.setInterval(() => {
      if (channels.length === 0) ask();
    }, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [api, runId, callId, live, channels.length]);

  const pull = useCallback(async (): Promise<boolean> => {
    if (channel === "") return false;
    const slice = await api.streamWindow(runId, callId, channel, offset.current);
    if (slice.end > offset.current) {
      setText((current) => current + slice.text);
      offset.current = slice.end;
    }
    setTail(slice);
    return slice.closed;
  }, [api, runId, callId, channel]);

  // смена канала — чтение с начала; живой хвост дочитывается раз в секунду до закрытия
  useEffect(() => {
    offset.current = 0;
    setText("");
    setTail(null);
    setError("");
    if (channel === "") return;

    let alive = true;
    let timer = 0;
    const step = (): void => {
      void pull().then(
        (closed) => {
          if (!alive) return;
          if (live && !closed) {
            timer = window.setTimeout(step, POLL_MS);
          }
        },
        (failure: unknown) => {
          if (alive) setError(errorText(failure));
        },
      );
    };
    step();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [channel, live, pull]);

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
