import { useCallback, useState } from "react";

export type StoredWidth = {
  /** Ширина, выбранная пользователем; undefined — из CSS. */
  width: number | undefined;
  /** Ширина, пока тянут; в конце — settle запоминает. */
  resize: (width: number) => void;
  settle: (width: number) => void;
};

/** Ширина колонки, выбранная пользователем ресайзом: хранится в браузере
 * под ключом и переживает перезагрузку; пределы задаёт CSS. */
export function useStoredWidth(key: string): StoredWidth {
  const [width, setWidth] = useState<number | undefined>(() => load(key));

  const resize = useCallback((next: number) => {
    setWidth(next);
  }, []);

  const settle = useCallback(
    (next: number) => {
      setWidth(next);
      save(key, next);
    },
    [key],
  );

  return { width, resize, settle };
}

function load(key: string): number | undefined {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(key);
  } catch {
    return undefined;
  }

  if (raw === null) {
    return undefined;
  }

  const width = Number(raw);
  if (!Number.isFinite(width) || width <= 0) {
    return undefined;
  }

  return width;
}

function save(key: string, width: number): void {
  try {
    window.localStorage.setItem(key, String(Math.round(width)));
  } catch {
    return;
  }
}
