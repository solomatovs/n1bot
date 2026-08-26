import { useCallback, useEffect, useState } from "react";

import { errorText, type Loadable } from "../components/Async";

/** Загрузка значения с перезапросом; ошибка — текстом, не исключением. */
export function useLoadable<T>(load: () => Promise<T>): [Loadable<T>, () => void] {
  const [state, setState] = useState<Loadable<T>>({ kind: "loading" });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setState({ kind: "loading" });
    load().then(
      (value) => {
        if (alive) {
          setState({ kind: "ready", value });
        }
      },
      (error: unknown) => {
        if (alive) {
          setState({ kind: "error", message: errorText(error) });
        }
      },
    );

    return () => {
      alive = false;
    };
  }, [load, tick]);

  const reload = useCallback(() => {
    setTick((n) => n + 1);
  }, []);

  return [state, reload];
}
