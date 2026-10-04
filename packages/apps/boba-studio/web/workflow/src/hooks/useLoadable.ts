import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../api/transport";
import type { Loadable } from "../components/Async";

type Options = {
  /** При перезапросе держать прежнее значение, а не мигать «loading». */
  keep?: boolean;
  /** Смена ключа перечитывает значение тем же загрузчиком. */
  key?: string;
};

/** Загрузка значения с перезапросом; ошибка — текстом, не исключением.
 * Ответ запроса, обогнанного новым (смена загрузчика, reload, размонтирование),
 * отбрасывается. */
export function useLoadable<T>(load: () => Promise<T>, options: Options = {}): [Loadable<T>, () => void] {
  const keep = options.keep === true;
  const key = options.key;
  const [state, setState] = useState<Loadable<T>>({ kind: "loading" });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setState((current) => {
      if (keep && current.kind === "ready") {
        return current;
      }

      return { kind: "loading" };
    });
    load().then(
      (value) => {
        if (alive) {
          setState({ kind: "ready", value });
        }
      },
      (error: unknown) => {
        if (alive) {
          setState({ kind: "error", message: ApiError.describe(error) });
        }
      },
    );

    return () => {
      alive = false;
    };
  }, [load, tick, keep, key]);

  const reload = useCallback(() => {
    setTick((n) => n + 1);
  }, []);

  return [state, reload];
}
